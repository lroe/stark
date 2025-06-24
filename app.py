import os
import json
import uuid
import base64
import google.generativeai as genai
from flask import Flask, request, render_template, jsonify, url_for, flash, redirect, session, abort
from flask_login import LoginManager, UserMixin, login_user, logout_user, current_user, login_required
from dotenv import load_dotenv
import datetime
from flask_wtf.csrf import CSRFProtect
from functools import wraps
import requests

# --- Firebase Admin SDK Initialization ---
import firebase_admin
from firebase_admin import credentials, firestore, auth

# --- Initialization ---
load_dotenv()
app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv("SECRET_KEY", "a-strong-default-secret-key-for-dev")
app.config['UPLOAD_FOLDER'] = 'static/uploads'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024 
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'mp3', 'wav', 'ogg'}

csrf = CSRFProtect(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

# --- Firebase Initialization ---
db = None
FIREBASE_WEB_API_KEY = os.getenv("FIREBASE_WEB_API_KEY")

try:
    firebase_creds_b64 = os.getenv('FIREBASE_ADMIN_SDK_BASE64')
    if firebase_creds_b64:
        decoded_creds_bytes = base64.b64decode(firebase_creds_b64)
        cred_dict = json.loads(decoded_creds_bytes.decode('utf-8'))
        cred = credentials.Certificate(cred_dict)
    else:
        cred = credentials.Certificate('firebase-adminsdk.json')
    
    firebase_admin.initialize_app(cred)
    db = firestore.client()
    print("Firebase Admin SDK initialized successfully.")
except Exception as e:
    print(f"FATAL ERROR: Could not initialize Firebase Admin SDK: {e}")

if os.getenv("GEMINI_API_KEY"):
    genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

RAG_RETRIEVERS = {}

# --- User and Auth Management ---
class User(UserMixin):
    def __init__(self, uid, user_data):
        self.id = uid
        self.uid = uid
        self.username = user_data.get('username')
        self.is_admin = user_data.get('is_admin', False)

    def is_enrolled(self, course):
        if not db or not course or not course.get('id'): return False
        enrollment_ref = db.collection('enrollments').where('user_id', '==', self.uid).where('course_id', '==', course['id']).limit(1).stream()
        return len(list(enrollment_ref)) > 0

@login_manager.user_loader
def load_user(user_id):
    if not db: return None
    try:
        user_doc = db.collection('users').document(user_id).get()
        if user_doc.exists:
            return User(user_id, user_doc.to_dict())
    except Exception as e:
        print(f"Error loading user {user_id}: {e}")
    return None

# --- Helper Functions & Decorators ---
def _doc_to_dict(doc):
    if not doc.exists: return None
    doc_dict = doc.to_dict()
    doc_dict['id'] = doc.id
    return doc_dict

def check_db_connection(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not db:
            flash("The application is not connected to the database. Please contact the administrator.", "danger")
            return render_template('explore.html', courses=[])
        return f(*args, **kwargs)
    return decorated_function
    
def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            flash("You do not have permission to access this page.", "danger")
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated_function

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# --- AI and Parsing Functions ---
PARSER_PROMPT = """
You are a precise curriculum parsing agent. Your task is to convert a teacher's lesson script into a structured JSON object. You MUST follow these rules exactly.
1. The final JSON object MUST have a single top-level key: "steps".
2. For explanatory text, create a "CONTENT" step with a "text" key.
3. For image tags like [IMAGE: alt="A picture."], create a "MEDIA" step with "alt_text" and a "media_type" of "image".
4. For audio tags like [AUDIO: description="A sound."], create a "MEDIA" step with "alt_text" (using the description) and a "media_type" of "audio".
5. For multiple-choice questions like [QUESTION: ... OPTIONS: A)... ANSWER: B], create a "QUESTION_MCQ" step.
6. For short-answer questions like [QUESTION_SA: ... KEYWORDS: word1, word2, ...], create a "QUESTION_SA" step.

Parse the following script:
"""
TUTOR_PROMPT_TEMPLATE = {
    "CONTENT": "You are a friendly and engaging tutor. Your task is to teach the following information to a student. Your goal is to be comprehensive and ensure no details are lost. Explain the provided text clearly, including all examples and specific terms mentioned. After explaining the content, ask a simple question to prompt the user to continue, like 'Does that make sense?' or 'Shall we move on?'.\n\nHere is the text to explain:\n---\n{}\n---",
    "MEDIA_IMAGE": "An image with the description '{}' has just been shown. Briefly call the student's attention to it and ask if they are ready to continue.",
    "MEDIA_AUDIO": "An audio clip with the description '{}' is available to play. Briefly encourage the student to listen to it and ask if they are ready to continue when they're done.",
    "QUESTION": "Okay, time for a quick question to check your understanding: {}"
}
INTENT_CLASSIFIER_PROMPT = "You are an intent classification agent. Your task is to analyze a user's input during a lesson and determine their intent. The user's input is: '{}'. The available media descriptions in this lesson are: {}. You MUST respond with a single, specific JSON object. Choose ONE of the following intents:\n\n1.  If the user is asking a general question about the lesson content, respond with:\n    {{\"intent\": \"QNA\", \"query\": \"the user's original question\"}}\n\n2.  If the user is asking to see a specific piece of media again AND their request matches one of the available media descriptions, respond with:\n    {{\"intent\": \"MEDIA_REQUEST\", \"alt_text\": \"the matching media description from the list\"}}\n\n3.  If the user's request is unclear or doesn't fit the above, default to a general question:\n    {{\"intent\": \"QNA\", \"query\": \"the user's original question\"}}\n\nUser Input: '{}'"

def get_tutor_response(full_prompt):
    if not os.getenv("GEMINI_API_KEY"): return "AI Tutor is not configured."
    try:
        model = genai.GenerativeModel('gemini-1.5-pro-latest')
        response = model.generate_content(full_prompt)
        return response.text if response.text else "Let's try that another way."
    except Exception as e:
        print(f"Error getting tutor response: {e}")
        return "I seem to be having a little trouble thinking. Could you try again?"

def parse_lesson_script(script_text):
    if not os.getenv("GEMINI_API_KEY"):
        print("Warning: GEMINI_API_KEY not set. Using basic parser.")
        return {'steps': [{'type': 'CONTENT', 'text': script_text}]}
    try:
        model = genai.GenerativeModel('gemini-1.5-pro-latest')
        response = model.generate_content(PARSER_PROMPT + script_text)
        cleaned_response = response.text.strip().replace("```json", "").replace("```", "").strip()
        parsed_json = json.loads(cleaned_response)
        return parsed_json if isinstance(parsed_json, dict) and 'steps' in parsed_json else None
    except Exception as e:
        print(f"Error during parsing: {e}")
        return None

def _get_or_create_rag_retriever(lesson_id, lesson_script):
    if lesson_id in RAG_RETRIEVERS: return RAG_RETRIEVERS[lesson_id]
    if not os.getenv("GEMINI_API_KEY"): return None
    text_chunks = [chunk for chunk in lesson_script.split('\n\n') if chunk.strip()]
    if not text_chunks: return None
    try:
        result = genai.embed_content(model='models/text-embedding-004', content=text_chunks, task_type="RETRIEVAL_DOCUMENT")
        embeddings = result['embedding']
        rag_data = [{'text': chunk, 'embedding': embeddings[i]} for i, chunk in enumerate(text_chunks)]
        RAG_RETRIEVERS[lesson_id] = rag_data
        return rag_data
    except Exception as e:
        print(f"Error creating RAG embeddings: {e}")
        return None

def answer_question_with_rag(question, rag_data):
    if not rag_data: return "I'm sorry, I don't have enough information to answer that."
    try:
        query_embedding = genai.embed_content(model='models/text-embedding-004', content=question, task_type="RETRIEVAL_QUERY")['embedding']
    except Exception as e:
        print(f"Error embedding RAG query: {e}")
        return "I had trouble understanding your question. Please try rephrasing."

    def dot_product(v1, v2): return sum(x*y for x, y in zip(v1, v2))

    for item in rag_data:
        item['similarity'] = dot_product(item['embedding'], query_embedding)

    sorted_data = sorted(rag_data, key=lambda x: x['similarity'], reverse=True)
    top_chunks = [item['text'] for item in sorted_data[:3]]
    context = "\n---\n".join(top_chunks)
    
    rag_prompt = f"Based ONLY on the following context, provide a concise answer to the user's question. If the context doesn't contain the answer, say \"That's a great question, but it's not covered in this chapter's material.\"\n\nCONTEXT:\n{context}\n\nUSER'S QUESTION:\n{question}"
    return get_tutor_response(rag_prompt)

# --- Main Routes ---
@app.route('/')
def index():
    return redirect(url_for('explore'))

@app.route('/explore')
@check_db_connection
def explore():
    courses_query = db.collection('courses').where('status', '==', 'published').stream()
    courses = [_doc_to_dict(c) for c in courses_query]
    
    creator_ids = list({c['user_id'] for c in courses if 'user_id' in c})
    if creator_ids:
        creators_query = db.collection('users').where(firestore.FieldPath.document_id(), 'in', creator_ids).stream()
        creators = {cr.id: _doc_to_dict(cr) for cr in creators_query}
        for c in courses:
            c['creator'] = creators.get(c.get('user_id'))
    
    return render_template('explore.html', courses=courses)

@app.route('/course/<string:course_id>')
@check_db_connection
def course_detail_page(course_id):
    course_doc = db.collection('courses').document(course_id).get()
    if not course_doc.exists: abort(404)
    course = _doc_to_dict(course_doc)
    
    share_id = request.args.get('share_id')
    is_authorized = course['status'] == 'published' or \
                    (current_user.is_authenticated and (current_user.uid == course['user_id'] or current_user.is_admin)) or \
                    (course.get('shareable_link_id') and course['shareable_link_id'] == share_id)
    if not is_authorized: abort(404)
    
    creator_doc = db.collection('users').document(course['user_id']).get()
    course['creator'] = _doc_to_dict(creator_doc)
    
    lessons_query = db.collection('lessons').where('course_id', '==', course_id).order_by('chapter_number').stream()
    course['lessons'] = [_doc_to_dict(l) for l in lessons_query]
    
    return render_template('course_detail.html', course=course, share_id=share_id)

# --- Authentication Routes ---
@app.route('/register', methods=['GET', 'POST'])
@check_db_connection
def register():
    if current_user.is_authenticated: return redirect(url_for('dashboard'))
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')

        users_ref = db.collection('users').where('username', '==', username).limit(1).stream()
        if len(list(users_ref)) > 0:
            flash('Username already exists.', 'warning')
            return redirect(url_for('register'))

        try:
            email = f"{username.lower().replace(' ', '_')}@coursewell.app"
            user_record = auth.create_user(email=email, password=password, display_name=username)
            db.collection('users').document(user_record.uid).set({'username': username, 'is_admin': False, 'created_at': firestore.SERVER_TIMESTAMP})
            flash('Account created successfully! Please log in.', 'success')
            return redirect(url_for('login'))
        except Exception as e:
            flash(f"An error occurred during registration: {e}", 'danger')
            return redirect(url_for('register'))

    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if not db:
        flash("Database not connected. Login is temporarily disabled.", "danger")
        return render_template('login.html')
        
    if current_user.is_authenticated: return redirect(url_for('dashboard'))
    if request.method == 'POST':
        username, password = request.form.get('username'), request.form.get('password')
        if not FIREBASE_WEB_API_KEY:
            flash('Firebase Web API Key is not configured. Login is disabled.', 'danger')
            return redirect(url_for('login'))
            
        email = f"{username.lower().replace(' ', '_')}@coursewell.app"
        rest_api_url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={FIREBASE_WEB_API_KEY}"
        
        try:
            response = requests.post(rest_api_url, json={"email": email, "password": password, "returnSecureToken": True})
            response.raise_for_status()
            user_id = response.json()['localId']
            user = load_user(user_id)
            if user:
                login_user(user, remember=True)
                return redirect(request.args.get('next') or url_for('dashboard'))
            else:
                flash('Could not find user data after login.', 'danger')
        except requests.exceptions.HTTPError:
            flash('Invalid username or password.', 'danger')
        except Exception as e:
            flash(f'An unexpected error occurred: {e}', 'danger')
        return redirect(url_for('login'))
        
    return render_template('login.html')

@app.route('/logout')
def logout():
    logout_user()
    session.clear()
    flash('You have been logged out.', 'info')
    return redirect(url_for('login'))


# --- Logged-in User Routes ---
@app.route('/dashboard')
@login_required
@check_db_connection
def dashboard():
    enrollments_query = db.collection('enrollments').where('user_id', '==', current_user.uid).stream()
    enrollments = [_doc_to_dict(e) for e in enrollments_query]

    course_ids = [e['course_id'] for e in enrollments if 'course_id' in e]
    if course_ids:
        courses_query = db.collection('courses').where(firestore.FieldPath.document_id(), 'in', course_ids).stream()
        courses = {c.id: _doc_to_dict(c) for c in courses_query}
        
        creator_ids = list({c['user_id'] for c in courses.values() if 'user_id' in c})
        if creator_ids:
            creators_query = db.collection('users').where(firestore.FieldPath.document_id(), 'in', creator_ids).stream()
            creators = {cr.id: _doc_to_dict(cr) for cr in creators_query}
            for c in courses.values(): c['creator'] = creators.get(c.get('user_id'))
        
        for e in enrollments:
            e['course'] = courses.get(e['course_id'])

    return render_template('dashboard.html', enrollments=enrollments)

@app.route('/creator')
@login_required
@check_db_connection
def creator_dashboard():
    courses_query = db.collection('courses').where('user_id', '==', current_user.uid).stream()
    created_courses = [_doc_to_dict(c) for c in courses_query]
    return render_template('creator_dashboard.html', created_courses=created_courses)

@app.route('/course/create', methods=['POST'])
@login_required
@check_db_connection
def create_course():
    title = request.form.get('title')
    if not title:
        flash('A title is required to create a course.', 'warning')
        return redirect(url_for('creator_dashboard'))
    
    new_course_data = {
        'title': title, 'user_id': current_user.uid, 'status': 'draft', 'is_published': False,
        'description': '', 'thumbnail_url': None, 'shareable_link_id': None, 
        'created_at': firestore.SERVER_TIMESTAMP,
        'lesson_count': 0, 'review_count': 0, 'total_rating_sum': 0, 'average_rating': 0.0
    }
    _, course_ref = db.collection('courses').add(new_course_data)
    flash('Course created! You can now manage it.', 'success')
    return redirect(url_for('manage_course', course_id=course_ref.id))

@app.route('/course/<string:course_id>/manage')
@login_required
@check_db_connection
def manage_course(course_id):
    course_doc = db.collection('courses').document(course_id).get()
    if not course_doc.exists or course_doc.to_dict().get('user_id') != current_user.uid: abort(403)
    course = _doc_to_dict(course_doc)
    lessons_query = db.collection('lessons').where('course_id', '==', course_id).order_by('chapter_number').stream()
    course['lessons'] = [_doc_to_dict(l) for l in lessons_query]
    return render_template('manage_course.html', course=course)

@app.route('/course/<string:course_id>/add_chapter', methods=['GET'])
@login_required
@check_db_connection
def add_chapter_page(course_id):
    course_doc = db.collection('courses').document(course_id).get()
    if not course_doc.exists or course_doc.to_dict().get('user_id') != current_user.uid: abort(403)
    return render_template('create_chapter.html', course=_doc_to_dict(course_doc))

@app.route('/course/<string:course_id>/save_chapter', methods=['POST'])
@login_required
@check_db_connection
def save_chapter(course_id):
    course_ref = db.collection('courses').document(course_id)
    course_doc = course_ref.get()
    if not course_doc.exists or course_doc.to_dict().get('user_id') != current_user.uid: abort(403)
    
    script, title = request.form.get('script', ''), request.form.get('title', '')
    if not title or not script:
        flash('Both a title and script are required.', 'warning')
        return redirect(url_for('add_chapter_page', course_id=course_id))
    
    parsed_data = parse_lesson_script(script)
    if not parsed_data:
        flash('The AI could not understand the lesson structure. Please check your tags and try again.', 'danger')
        return redirect(url_for('add_chapter_page', course_id=course_id))
    
    last_chapter_query = db.collection('lessons').where('course_id', '==', course_id).order_by('chapter_number', direction=firestore.Query.DESCENDING).limit(1).stream()
    last_chapter_list = list(last_chapter_query)
    new_chapter_number = (last_chapter_list[0].to_dict()['chapter_number'] + 1) if last_chapter_list else 1

    new_lesson_data = {
        'title': title, 'raw_script': script, 'editor_html': request.form.get('editor_html'),
        'parsed_json': json.dumps(parsed_data), 'course_id': course_id, 'chapter_number': new_chapter_number
    }
    db.collection('lessons').add(new_lesson_data)
    
    course_ref.update({'lesson_count': firestore.Increment(1)})
    
    flash('Chapter added successfully!', 'success')
    return redirect(url_for('manage_course', course_id=course_id))

@app.route('/chapter/<string:lesson_id>/delete', methods=['POST'])
@login_required
@check_db_connection
def delete_chapter(lesson_id):
    lesson_ref = db.collection('lessons').document(lesson_id)
    lesson_doc = lesson_ref.get()
    if not lesson_doc.exists: abort(404)
    lesson = _doc_to_dict(lesson_doc)
    course_id = lesson['course_id']
    course_ref = db.collection('courses').document(course_id)
    course_doc = course_ref.get()
    if not course_doc.exists or course_doc.to_dict().get('user_id') != current_user.uid: abort(403)

    @firestore.transactional
    def delete_and_reorder_transaction(transaction):
        lesson_snapshot = lesson_ref.get(transaction=transaction)
        if not lesson_snapshot.exists: return
        deleted_chapter_number = lesson_snapshot.to_dict()['chapter_number']
        
        transaction.delete(lesson_ref)
        
        chapters_to_reorder_query = db.collection('lessons').where('course_id', '==', course_id).where('chapter_number', '>', deleted_chapter_number).stream()
        for chapter in chapters_to_reorder_query:
            new_num = chapter.to_dict()['chapter_number'] - 1
            transaction.update(chapter.reference, {'chapter_number': new_num})
        
        transaction.update(course_ref, {'lesson_count': firestore.Increment(-1)})

    transaction = db.transaction()
    delete_and_reorder_transaction(transaction)
    
    flash('Chapter deleted successfully.', 'success')
    return redirect(url_for('manage_course', course_id=course_id))

@app.route('/course/<string:course_id>/enroll', methods=['POST'])
@login_required
@check_db_connection
def enroll_in_course(course_id):
    course_doc = db.collection('courses').document(course_id).get()
    if not course_doc.exists: abort(404)
    course = _doc_to_dict(course_doc)

    if current_user.uid == course['user_id']:
        flash("You cannot enroll in a course you've created.", "warning")
        return redirect(url_for('course_detail_page', course_id=course_id))
    if current_user.is_enrolled(course):
        flash("You are already enrolled in this course.", "info")
        return redirect(url_for('course_player', course_id=course_id))
        
    new_enrollment = {'user_id': current_user.uid, 'course_id': course_id, 'last_completed_chapter_number': 0, 'completed_at': None}
    db.collection('enrollments').add(new_enrollment)
    
    flash(f"You have successfully enrolled in '{course['title']}'!", 'success')
    return redirect(url_for('course_player', course_id=course_id))

@app.route('/course/<string:course_id>/review', methods=['POST'])
@login_required
@check_db_connection
def submit_review(course_id):
    rating_str = request.form.get('rating')
    comment = request.form.get('comment')
    if not rating_str:
        flash("A star rating is required.", "warning")
        return redirect(url_for('certificate_view', course_id=course_id))
    
    rating = int(rating_str)
    course_ref = db.collection('courses').document(course_id)
    new_review_ref = db.collection('reviews').document()
    new_review_data = {
        'rating': rating, 'comment': comment, 'course_id': course_id, 
        'user_id': current_user.uid, 'created_at': firestore.SERVER_TIMESTAMP
    }

    @firestore.transactional
    def _update_course_review_stats(transaction):
        course_snapshot = course_ref.get(transaction=transaction)
        if not course_snapshot.exists:
            raise Exception("Course not found!")
        
        current_sum = course_snapshot.get('total_rating_sum') or 0
        current_count = course_snapshot.get('review_count') or 0

        new_sum = current_sum + rating
        new_count = current_count + 1
        new_average = new_sum / new_count if new_count > 0 else 0

        transaction.set(new_review_ref, new_review_data)
        transaction.update(course_ref, {
            'total_rating_sum': new_sum,
            'review_count': new_count,
            'average_rating': new_average
        })

    try:
        transaction = db.transaction()
        _update_course_review_stats(transaction)
        flash("Thank you for your feedback!", "success")
    except Exception as e:
        flash(f"An error occurred while submitting your review: {e}", "danger")

    return redirect(url_for('reviews_page', course_id=course_id))

@app.route('/course/<string:course_id>/reviews')
@check_db_connection
def reviews_page(course_id):
    course_doc = db.collection('courses').document(course_id).get();
    if not course_doc.exists: abort(404)
    course = _doc_to_dict(course_doc)
    reviews_query = db.collection('reviews').where('course_id', '==', course_id).order_by('created_at', direction=firestore.Query.DESCENDING).stream()
    reviews = [_doc_to_dict(r) for r in reviews_query]

    user_ids = {r['user_id'] for r in reviews}
    if user_ids:
        users_query = db.collection('users').where(firestore.FieldPath.document_id(), 'in', list(user_ids)).stream()
        users = {u.id: _doc_to_dict(u) for u in users_query}
        for r in reviews: r['user'] = users.get(r['user_id'])
    
    return render_template('reviews.html', course=course, reviews=reviews)

@app.route('/course/<string:course_id>/player')
@login_required
@check_db_connection
def course_player(course_id):
    enrollment_query = db.collection('enrollments').where('user_id', '==', current_user.uid).where('course_id', '==', course_id).limit(1).stream()
    enrollment_list = list(enrollment_query)
    enrollment = _doc_to_dict(enrollment_list[0]) if enrollment_list else None

    chapter_to_start = (enrollment['last_completed_chapter_number'] + 1) if enrollment else 1
    
    lessons_query = db.collection('lessons').where('course_id', '==', course_id).order_by('chapter_number').limit(int(chapter_to_start)).stream()
    lessons = [_doc_to_dict(l) for l in lessons_query]
    
    course_doc = db.collection('courses').document(course_id).get()
    if not lessons:
        if current_user.is_authenticated and current_user.uid == course_doc.to_dict().get('user_id'):
            flash('This course has no chapters yet. Add one to enable the preview.', 'info')
            return redirect(url_for('manage_course', course_id=course_id))
        flash("This course has no content yet.", "warning")
        return redirect(url_for('explore'))
        
    target_lesson = lessons[-1] if lessons else None
    
    if not target_lesson:
        flash("Could not find a valid chapter to start.", "warning")
        return redirect(url_for('dashboard'))

    return redirect(url_for('student_chapter_view', course_id=course_id, chapter_number=target_lesson['chapter_number']))


@app.route('/course/<string:course_id>/<int:chapter_number>')
@login_required
@check_db_connection
def student_chapter_view(course_id, chapter_number):
    course_doc = db.collection('courses').document(course_id).get()
    if not course_doc.exists: abort(404)
    course = _doc_to_dict(course_doc)
    
    lesson_query = db.collection('lessons').where('course_id', '==', course_id).where('chapter_number', '==', chapter_number).limit(1).stream()
    lesson_list = list(lesson_query)
    if not lesson_list: abort(404)
    lesson = _doc_to_dict(lesson_list[0])
    
    enrollment_query = db.collection('enrollments').where('user_id', '==', current_user.uid).where('course_id', '==', course_id).limit(1).stream()
    enrollment_list = list(enrollment_query)
    enrollment = _doc_to_dict(enrollment_list[0]) if enrollment_list else None
    
    is_authorized = course['status'] == 'published' or (current_user.is_authenticated and (current_user.uid == course['user_id'] or enrollment or current_user.is_admin))
    if not is_authorized: abort(404)

    all_lessons_query = db.collection('lessons').where('course_id', '==', course_id).order_by('chapter_number').stream()
    course['lessons'] = [_doc_to_dict(l) for l in all_lessons_query]

    initial_history_data = None
    if enrollment:
        history_query = db.collection('chat_histories').where('enrollment_id', '==', enrollment['id']).where('lesson_id', '==', lesson['id']).limit(1).stream()
        history_list = list(history_query)
        if history_list: initial_history_data = _doc_to_dict(history_list[0])
    else: 
        session_key = f'preview_chat_{lesson["id"]}'
        if session_key in session: del session[session_key]
            
    return render_template('course_player.html', course=course, current_lesson=lesson, enrollment=enrollment, initial_history=initial_history_data)

@app.route('/course/<string:course_id>/certificate')
@login_required
@check_db_connection
def certificate_view(course_id):
    enrollment_query = db.collection('enrollments').where('user_id', '==', current_user.uid).where('course_id', '==', course_id).limit(1).stream()
    enrollment_list = list(enrollment_query)
    if not enrollment_list: abort(404)
    enrollment = _doc_to_dict(enrollment_list[0])
    
    if not enrollment.get('completed_at'):
        flash("You have not completed this course yet.", "warning")
        return redirect(url_for('course_player', course_id=course_id))
    
    enrollment['user'] = _doc_to_dict(db.collection('users').document(enrollment['user_id']).get())
    enrollment['course'] = _doc_to_dict(db.collection('courses').document(enrollment['course_id']).get())

    review_query = db.collection('reviews').where('user_id', '==', current_user.uid).where('course_id', '==', course_id).limit(1).stream()
    review_list = list(review_query)
    existing_review = _doc_to_dict(review_list[0]) if review_list else None

    return render_template('certificate.html', enrollment=enrollment, existing_review=existing_review)

@app.route('/course/<string:course_id>/update_details', methods=['POST'])
@login_required
@check_db_connection
def update_course_details(course_id):
    course_ref = db.collection('courses').document(course_id)
    course_doc = course_ref.get()
    if not course_doc.exists or course_doc.to_dict().get('user_id') != current_user.uid: abort(403)
    
    update_data = {'description': request.form.get('description')}
    if 'thumbnail' in request.files:
        file = request.files['thumbnail']
        if file and file.filename != '':
            filename = str(uuid.uuid4()) + os.path.splitext(file.filename)[1]
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(filepath)
            update_data['thumbnail_url'] = url_for('static', filename=f'uploads/{filename}', _external=True)

    if update_data:
        course_ref.update(update_data)
        flash('Course details updated successfully!', 'success')
        
    return redirect(url_for('manage_course', course_id=course_id))

@app.route('/chapter/<string:lesson_id>/edit')
@login_required
@check_db_connection
def edit_chapter_page(lesson_id):
    lesson_doc = db.collection('lessons').document(lesson_id).get()
    if not lesson_doc.exists: abort(404)
    lesson = _doc_to_dict(lesson_doc)
    course_doc = db.collection('courses').document(lesson['course_id']).get()
    if not course_doc.exists or course_doc.to_dict().get('user_id') != current_user.uid: abort(403)
    lesson['course'] = _doc_to_dict(course_doc)
    return render_template('edit_chapter.html', lesson=lesson)

@app.route('/chapter/<string:lesson_id>/update', methods=['POST'])
@login_required
@check_db_connection
def update_chapter(lesson_id):
    lesson_ref = db.collection('lessons').document(lesson_id)
    lesson_doc = lesson_ref.get()
    if not lesson_doc.exists: abort(404)
    lesson = _doc_to_dict(lesson_doc)
    
    course_doc = db.collection('courses').document(lesson['course_id']).get()
    if not course_doc.exists or course_doc.to_dict().get('user_id') != current_user.uid: abort(403)
    
    parsed_data = parse_lesson_script(request.form['script'])
    if not parsed_data:
        flash('The AI could not understand the lesson structure.', 'danger')
        return redirect(url_for('edit_chapter_page', lesson_id=lesson_id))

    lesson_ref.update({
        'title': request.form['title'], 'raw_script': request.form['script'], 'editor_html': request.form.get('editor_html'),
        'parsed_json': json.dumps(parsed_data)
    })
    
    flash('Chapter updated successfully!', 'success')
    return redirect(url_for('manage_course', course_id=lesson['course_id']))

@app.route('/course/<string:course_id>/submit_for_review', methods=['POST'])
@login_required
@check_db_connection
def submit_for_review(course_id):
    course_ref = db.collection('courses').document(course_id)
    course_doc = course_ref.get()
    if course_doc.exists and course_doc.to_dict().get('user_id') == current_user.uid:
        course_ref.update({'status': 'pending_review'})
        flash('Course submitted for review!', 'success')
    return redirect(url_for('manage_course', course_id=course_id))

@app.route('/course/<string:course_id>/unpublish', methods=['POST'])
@login_required
@check_db_connection
def unpublish_course(course_id):
    course_ref = db.collection('courses').document(course_id)
    course_doc = course_ref.get()
    if course_doc.exists and (course_doc.to_dict().get('user_id') == current_user.uid or current_user.is_admin):
        course_ref.update({'status': 'draft'})
        flash('Course returned to draft status.', 'info')
    return redirect(url_for('manage_course', course_id=course_id))

@app.route('/admin/dashboard')
@login_required
@admin_required
@check_db_connection
def admin_dashboard():
    pending_courses_query = db.collection('courses').where('status', '==', 'pending_review').stream()
    pending_courses = [_doc_to_dict(c) for c in pending_courses_query]
    
    creator_ids = list({c['user_id'] for c in pending_courses if 'user_id' in c})
    if creator_ids:
        creators_query = db.collection('users').where(firestore.FieldPath.document_id(), 'in', creator_ids).stream()
        creators = {cr.id: _doc_to_dict(cr) for cr in creators_query}
        for course in pending_courses:
            course['creator'] = creators.get(course['user_id'])
            
    return render_template('admin_dashboard.html', pending_courses=pending_courses)

@app.route('/admin/course/<string:course_id>/decide', methods=['POST'])
@login_required
@admin_required
@check_db_connection
def decide_course(course_id):
    course_ref = db.collection('courses').document(course_id)
    course_doc = course_ref.get()
    if not course_doc.exists: abort(404)
    decision = request.form.get('decision')
    
    if decision == 'approve':
        course_ref.update({'status': 'published'})
        flash('Course approved and published.', 'success')
    elif decision == 'reject':
        course_ref.update({'status': 'rejected'})
        flash('Course rejected and returned to creator.', 'warning')
        
    return redirect(url_for('admin_dashboard'))

@app.route('/course/<string:course_id>/generate_link', methods=['POST'])
@login_required
@check_db_connection
def generate_share_link(course_id):
    course_ref = db.collection('courses').document(course_id)
    course_doc = course_ref.get()
    if course_doc.exists and course_doc.to_dict().get('user_id') == current_user.uid:
        if not course_doc.to_dict().get('shareable_link_id'):
            course_ref.update({'shareable_link_id': str(uuid.uuid4())})
    return redirect(url_for('manage_course', course_id=course_id))

@app.route('/share/<string:link_id>')
@check_db_connection
def shared_course_view(link_id):
    course_query = db.collection('courses').where('shareable_link_id', '==', link_id).limit(1).stream()
    course_list = list(course_query)
    if not course_list: abort(404)
    course = _doc_to_dict(course_list[0])
    return redirect(url_for('course_detail_page', course_id=course['id'], share_id=link_id))
    
# --- Chat Routes ---
@app.route('/chat/intent', methods=['POST'])
@login_required
@check_db_connection
def classify_intent():
    data = request.json
    user_input = data.get('user_input')
    lesson_id = data.get('lesson_id')

    lesson_doc = db.collection('lessons').document(lesson_id).get()
    if not lesson_doc.exists: abort(404)
    lesson = _doc_to_dict(lesson_doc)

    lesson_steps = json.loads(lesson['parsed_json']).get('steps', [])
    media_descriptions = [step.get('alt_text') for step in lesson_steps if step.get('type') == 'MEDIA' and step.get('alt_text')]
    prompt = INTENT_CLASSIFIER_PROMPT.format(user_input, media_descriptions, user_input)
    
    try:
        model = genai.GenerativeModel('gemini-1.5-pro-latest')
        response = model.generate_content(prompt)
        cleaned_response = response.text.strip().replace("```json", "").replace("```", "").strip()
        intent_data = json.loads(cleaned_response)
        return jsonify(intent_data)
    except Exception as e:
        print(f"Error classifying intent: {e}")
        return jsonify({"intent": "QNA", "query": user_input})


@app.route('/chat', methods=['POST'])
@login_required
@check_db_connection
def chat():
    data = request.json
    lesson_id = data.get('lesson_id')
    user_input = data.get('user_input')
    request_type = data.get('request_type', 'LESSON_FLOW')

    lesson_doc = db.collection('lessons').document(lesson_id).get()
    if not lesson_doc.exists: abort(404)
    lesson = _doc_to_dict(lesson_doc)
    
    course_doc = db.collection('courses').document(lesson['course_id']).get()
    course = _doc_to_dict(course_doc)
    
    lessons_query = db.collection('lessons').where('course_id', '==', course['id']).order_by('chapter_number').stream()
    course['lessons'] = [_doc_to_dict(l) for l in lessons_query]

    lesson_steps = json.loads(lesson['parsed_json']).get('steps', [])

    if request_type == 'MEDIA_REQUEST':
        requested_alt_text = user_input
        media_to_show = next((step for step in lesson_steps if step.get('type') == 'MEDIA' and step.get('alt_text') == requested_alt_text), None)
        if media_to_show:
            return jsonify({
                "tutor_text": f"Of course, here is '{media_to_show.get('alt_text')}' again.",
                "media_url": media_to_show.get('media_url'), "media_type": media_to_show.get('media_type'),
                "is_qna_response": True
            })
        else:
            request_type = 'QNA'

    enrollment_query = db.collection('enrollments').where('user_id', '==', current_user.uid).where('course_id', '==', course['id']).limit(1).stream()
    enrollment_list = list(enrollment_query)
    enrollment_doc = enrollment_list[0] if enrollment_list else None
    enrollment = _doc_to_dict(enrollment_doc) if enrollment_doc else None
    
    is_creator = (current_user.uid == course['user_id'])
    if not enrollment and not is_creator and not current_user.is_admin: abort(403)

    history_record_ref, step_index, chunk_index, chat_log = None, 0, 0, []
    session_key = f'preview_chat_{lesson_id}'

    if enrollment:
        history_query = db.collection('chat_histories').where('enrollment_id', '==', enrollment_doc.id).where('lesson_id', '==', lesson_id).limit(1).stream()
        history_list = list(history_query)
        if history_list:
            history_record_ref = history_list[0].reference
            history_record = _doc_to_dict(history_list[0])
            step_index = history_record.get('current_step_index', 0)
            chunk_index = history_record.get('current_chunk_index', 0)
            chat_log = json.loads(history_record.get('history_json', '[]'))
        else:
            history_record_ref = db.collection('chat_histories').document()
    else:
        if session_key in session and user_input is not None:
             chat_log, step_index, chunk_index = session[session_key].get('chat_log', []), session[session_key].get('step_index', 0), session[session_key].get('chunk_index', 0)
        else:
             session[session_key] = {'step_index': 0, 'chunk_index': 0, 'chat_log': []}

    if user_input and request_type == 'LESSON_FLOW' and user_input != 'Continue':
        chat_log.append({"sender": "student", "type": "text", "content": user_input})
    
    if request_type == 'QNA':
        chat_log.append({"sender": "student", "type": "text", "content": user_input})
        retriever = _get_or_create_rag_retriever(lesson['id'], lesson['raw_script'])
        response_text = answer_question_with_rag(user_input, retriever)
        chat_log.append({"sender": "tutor", "type": "text", "content": response_text})
        
        if history_record_ref: 
            if history_record_ref.get().exists: history_record_ref.update({'history_json': json.dumps(chat_log)})
            else: history_record_ref.set({'enrollment_id': enrollment_doc.id, 'lesson_id': lesson_id, 'history_json': json.dumps(chat_log), 'current_step_index': 0, 'current_chunk_index': 0})
        else: session[session_key]['chat_log'] = chat_log; session.modified = True
        
        return jsonify({'is_qna_response': True, 'tutor_text': response_text})

    response_data, model_response_text = {}, ""
    next_step_index, next_chunk_index = step_index, chunk_index

    if step_index >= len(lesson_steps):
        response_data['is_lesson_end'] = True
        model_response_text = "Congratulations! You've completed this chapter."
        if enrollment and enrollment['last_completed_chapter_number'] < lesson['chapter_number']:
            enrollment_doc.reference.update({'last_completed_chapter_number': lesson['chapter_number']})
            if lesson['chapter_number'] >= len(course['lessons']):
                if not enrollment.get('completed_at'):
                    enrollment_doc.reference.update({'completed_at': firestore.SERVER_TIMESTAMP})
                response_data['certificate_url'] = url_for('certificate_view', course_id=course['id'])
            else:
                next_chapter = next((l for l in course['lessons'] if l['chapter_number'] == lesson['chapter_number'] + 1), None)
                if next_chapter:
                    response_data['next_chapter_url'] = url_for('student_chapter_view', course_id=course['id'], chapter_number=next_chapter['chapter_number'])
    else:
        current_step = lesson_steps[step_index]
        step_type = current_step.get('type')

        if step_type == 'CONTENT':
            content_chunks = [chunk for chunk in current_step.get('text', '').split('\n\n') if chunk.strip()]
            if chunk_index < len(content_chunks):
                prompt = TUTOR_PROMPT_TEMPLATE['CONTENT'].format(content_chunks[chunk_index])
                model_response_text = get_tutor_response(prompt)
                chat_log.append({"sender": "tutor", "type": "text", "content": model_response_text})
                next_chunk_index = chunk_index + 1
            if next_chunk_index >= len(content_chunks):
                next_step_index, next_chunk_index = step_index + 1, 0
        else:
            if step_type == 'MEDIA':
                media_type = current_step.get('media_type', 'image')
                prompt_template = TUTOR_PROMPT_TEMPLATE.get(f"MEDIA_{media_type.upper()}", TUTOR_PROMPT_TEMPLATE['MEDIA_IMAGE'])
                model_response_text = get_tutor_response(prompt_template.format(current_step.get('alt_text', '')))
                chat_log.append({"sender": "tutor", "type": "text", "content": model_response_text})
                chat_log.append({"sender": "tutor", "type": media_type, "url": current_step.get('media_url'), "alt": current_step.get('alt_text')})
                response_data.update({'media_url': current_step.get('media_url'), 'media_type': media_type})
            elif step_type in ['QUESTION_MCQ', 'QUESTION_SA']:
                model_response_text = get_tutor_response(TUTOR_PROMPT_TEMPLATE['QUESTION'].format(current_step.get('question', '')))
                chat_log.append({"sender": "tutor", "type": "text", "content": model_response_text})
                response_data['question'] = current_step
            
            next_step_index, next_chunk_index = step_index + 1, 0

    if model_response_text: response_data['tutor_text'] = model_response_text

    update_payload = {'current_step_index': next_step_index, 'current_chunk_index': next_chunk_index, 'history_json': json.dumps(chat_log)}
    if history_record_ref:
        if history_record_ref.get().exists: history_record_ref.update(update_payload)
        else: history_record_ref.set({**update_payload, 'enrollment_id': enrollment_doc.id, 'lesson_id': lesson_id})
    else:
        session[session_key] = update_payload
        session.modified = True
    
    return jsonify(response_data)

@app.route('/chat/reset', methods=['POST'])
@login_required
@check_db_connection
def reset_conversation():
    lesson_id = request.json.get('lesson_id')
    lesson_doc = db.collection('lessons').document(lesson_id).get()
    if not lesson_doc.exists: abort(404)
    lesson = _doc_to_dict(lesson_doc)

    enrollment_query = db.collection('enrollments').where('user_id', '==', current_user.uid).where('course_id', '==', lesson['course_id']).limit(1).stream()
    enrollment_list = list(enrollment_query)
    if enrollment_list:
        enrollment_doc = enrollment_list[0]
        history_query = db.collection('chat_histories').where('enrollment_id', '==', enrollment_doc.id).where('lesson_id', '==', lesson_id).limit(1).stream()
        history_list = list(history_query)
        if history_list:
            history_list[0].reference.update({'current_step_index': 0, 'current_chunk_index': 0, 'history_json': '[]'})
    else: 
        session_key = f'preview_chat_{lesson_id}'
        if session_key in session: del session[session_key]
    return jsonify({'success': True})


@app.route('/chat/delete_last_turn', methods=['POST'])
@login_required
@check_db_connection
def delete_last_turn():
    lesson_id = request.json.get('lesson_id')
    flash("This feature is complex and not yet implemented.", "info")
    return jsonify({'success': False, 'message': 'Feature under development.'})


if __name__ == '__main__':
    app.run(debug=True)
