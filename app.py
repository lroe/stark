import os
import json
import uuid
import base64 # Import the base64 library
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
db = None # Initialize db as None
FIREBASE_WEB_API_KEY = os.getenv("FIREBASE_WEB_API_KEY")

try:
    # Check for the Vercel environment variable FIRST
    firebase_creds_b64 = os.getenv('FIREBASE_ADMIN_SDK_BASE64')
    if firebase_creds_b64:
        print("Found FIREBASE_ADMIN_SDK_BASE64 env var. Decoding...")
        decoded_creds_bytes = base64.b64decode(firebase_creds_b64)
        cred_dict = json.loads(decoded_creds_bytes.decode('utf-8'))
        cred = credentials.Certificate(cred_dict)
        print("Successfully parsed decoded credentials.")
    else:
        # Fallback for local development using the JSON file
        print("Using local firebase-adminsdk.json file.")
        cred = credentials.Certificate('firebase-adminsdk.json')
    
    firebase_admin.initialize_app(cred)
    print("Firebase Admin SDK initialized successfully.")
    db = firestore.client() # Assign db client here
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
                    (current_user.is_authenticated and (current_user.id == course['user_id'] or current_user.is_admin)) or \
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
    enrollments_query = db.collection('enrollments').where('user_id', '==', current_user.id).stream()
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
    courses_query = db.collection('courses').where('user_id', '==', current_user.id).stream()
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
        'title': title, 'user_id': current_user.id, 'status': 'draft', 'is_published': False,
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
    if not course_doc.exists or course_doc.to_dict().get('user_id') != current_user.id: abort(403)
    course = _doc_to_dict(course_doc)
    lessons_query = db.collection('lessons').where('course_id', '==', course_id).order_by('chapter_number').stream()
    course['lessons'] = [_doc_to_dict(l) for l in lessons_query]
    return render_template('manage_course.html', course=course)

@app.route('/course/<string:course_id>/add_chapter', methods=['GET'])
@login_required
@check_db_connection
def add_chapter_page(course_id):
    course_doc = db.collection('courses').document(course_id).get()
    if not course_doc.exists or course_doc.to_dict().get('user_id') != current_user.id: abort(403)
    return render_template('create_chapter.html', course=_doc_to_dict(course_doc))

@app.route('/course/<string:course_id>/save_chapter', methods=['POST'])
@login_required
@check_db_connection
def save_chapter(course_id):
    course_ref = db.collection('courses').document(course_id)
    course_doc = course_ref.get()
    if not course_doc.exists or course_doc.to_dict().get('user_id') != current_user.id: abort(403)
    
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
    if not course_doc.exists or course_doc.to_dict().get('user_id') != current_user.id: abort(403)

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

    if current_user.id == course['user_id']:
        flash("You cannot enroll in a course you've created.", "warning")
        return redirect(url_for('course_detail_page', course_id=course_id))
    if current_user.is_enrolled(course):
        flash("You are already enrolled in this course.", "info")
        return redirect(url_for('course_player', course_id=course_id))
        
    new_enrollment = {'user_id': current_user.id, 'course_id': course_id, 'last_completed_chapter_number': 0, 'completed_at': None}
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
        'user_id': current_user.id, 'created_at': firestore.SERVER_TIMESTAMP
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
    enrollment_query = db.collection('enrollments').where('user_id', '==', current_user.id).where('course_id', '==', course_id).limit(1).stream()
    enrollment_list = list(enrollment_query)
    enrollment = _doc_to_dict(enrollment_list[0]) if enrollment_list else None

    chapter_to_start = (enrollment['last_completed_chapter_number'] + 1) if enrollment else 1
    
    lessons_query = db.collection('lessons').where('course_id', '==', course_id).order_by('chapter_number').limit(int(chapter_to_start)).stream()
    lessons = [_doc_to_dict(l) for l in lessons_query]
    
    if not lessons:
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
    
    enrollment_query = db.collection('enrollments').where('user_id', '==', current_user.id).where('course_id', '==', course_id).limit(1).stream()
    enrollment_list = list(enrollment_query)
    enrollment = _doc_to_dict(enrollment_list[0]) if enrollment_list else None
    
    is_authorized = course['status'] == 'published' or (current_user.is_authenticated and (current_user.id == course['user_id'] or enrollment or current_user.is_admin))
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
    enrollment_query = db.collection('enrollments').where('user_id', '==', current_user.id).where('course_id', '==', course_id).limit(1).stream()
    enrollment_list = list(enrollment_query)
    if not enrollment_list: abort(404)
    enrollment = _doc_to_dict(enrollment_list[0])
    
    if not enrollment.get('completed_at'):
        flash("You have not completed this course yet.", "warning")
        return redirect(url_for('course_player', course_id=course_id))
    
    enrollment['user'] = _doc_to_dict(db.collection('users').document(enrollment['user_id']).get())
    enrollment['course'] = _doc_to_dict(db.collection('courses').document(enrollment['course_id']).get())

    review_query = db.collection('reviews').where('user_id', '==', current_user.id).where('course_id', '==', course_id).limit(1).stream()
    review_list = list(review_query)
    existing_review = _doc_to_dict(review_list[0]) if review_list else None

    return render_template('certificate.html', enrollment=enrollment, existing_review=existing_review)

@app.route('/course/<string:course_id>/update_details', methods=['POST'])
@login_required
@check_db_connection
def update_course_details(course_id):
    course_ref = db.collection('courses').document(course_id)
    course_doc = course_ref.get()
    if not course_doc.exists or course_doc.to_dict().get('user_id') != current_user.id: abort(403)
    
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
    if not course_doc.exists or course_doc.to_dict().get('user_id') != current_user.id: abort(403)
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
    if not course_doc.exists or course_doc.to_dict().get('user_id') != current_user.id: abort(403)
    
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
    if course_doc.exists and course_doc.to_dict().get('user_id') == current_user.id:
        course_ref.update({'status': 'pending_review'})
        flash('Course submitted for review!', 'success')
    return redirect(url_for('manage_course', course_id=course_id))

@app.route('/course/<string:course_id>/unpublish', methods=['POST'])
@login_required
@check_db_connection
def unpublish_course(course_id):
    course_ref = db.collection('courses').document(course_id)
    course_doc = course_ref.get()
    if course_doc.exists and (course_doc.to_dict().get('user_id') == current_user.id or current_user.is_admin):
        course_ref.update({'status': 'draft'})
        flash('Course returned to draft status.', 'info')
    return redirect(url_for('manage_course', course_id=course_id))

@app.route('/admin/dashboard')
@login_required
@check_db_connection
def admin_dashboard():
    if not current_user.is_admin: abort(403)
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
@check_db_connection
def decide_course(course_id):
    if not current_user.is_admin: abort(403)
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
    if course_doc.exists and course_doc.to_dict().get('user_id') == current_user.id:
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
    
# --- Chat Routes (currently non-functional stubs) ---
@app.route('/chat', methods=['POST'])
def chat(): return jsonify({"error": "Chat not implemented"}), 501
@app.route('/chat/intent', methods=['POST'])
def classify_intent(): return jsonify({"intent": "QNA", "query": "test"})
@app.route('/chat/reset', methods=['POST'])
def reset_conversation(): return jsonify({'success': True})
@app.route('/chat/delete_last_turn', methods=['POST'])
def delete_last_turn(): return jsonify({'success': False, 'message': 'Not implemented'})

if __name__ == '__main__':
    app.run(debug=True)
