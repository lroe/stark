import os
import json
import uuid
import google.generativeai as genai
from flask import Flask, request, render_template, jsonify, url_for, flash, redirect, session, abort
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_login import LoginManager, UserMixin, login_user, logout_user, current_user, login_required
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
import datetime

from flask_wtf.csrf import CSRFProtect
from functools import wraps

# --- Initialization ---
load_dotenv()
app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv("SECRET_KEY", "a-strong-default-secret-key-for-dev")
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///coursewell.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = 'static/uploads'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024 
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'mp3', 'wav', 'ogg'}

db = SQLAlchemy(app)
csrf = CSRFProtect(app)
migrate = Migrate(app, db)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

RAG_RETRIEVERS = {}

# --- Database Models ---
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(128))
    is_admin = db.Column(db.Boolean, nullable=False, default=False)
    courses = db.relationship('Course', backref='creator', lazy=True, cascade="all, delete-orphan")
    enrollments = db.relationship('Enrollment', back_populates='user', lazy='dynamic', cascade="all, delete-orphan")
    reviews = db.relationship('Review', backref='user', lazy='dynamic')
    def set_password(self, password): self.password_hash = generate_password_hash(password)
    def check_password(self, password): return check_password_hash(self.password_hash, password)
    def is_enrolled(self, course): return self.enrollments.filter_by(course_id=course.id).count() > 0

class Course(db.Model):
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    title = db.Column(db.String(150), nullable=False)
    status = db.Column(db.String(20), nullable=False, default='draft')
    is_published = db.Column(db.Boolean, nullable=False, default=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    enrollees = db.relationship('Enrollment', back_populates='course', lazy='dynamic', cascade="all, delete-orphan")
    lessons = db.relationship('Lesson', backref='course', lazy=True, cascade="all, delete-orphan", order_by="Lesson.chapter_number")
    description = db.Column(db.Text, nullable=True)
    thumbnail_url = db.Column(db.String(255), nullable=True)
    reviews = db.relationship('Review', backref='course', lazy='dynamic')
    shareable_link_id = db.Column(db.String(36), unique=True, nullable=True)
    @property
    def average_rating(self):
        reviews = self.reviews.all()
        if not reviews: return 0
        return sum(r.rating for r in reviews) / len(reviews)

class Lesson(db.Model):
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    title = db.Column(db.String(150), nullable=False)
    raw_script = db.Column(db.Text, nullable=False)
    editor_html = db.Column(db.Text, nullable=True) 
    parsed_json = db.Column(db.Text, nullable=False)
    course_id = db.Column(db.String(36), db.ForeignKey('course.id'), nullable=False)
    chapter_number = db.Column(db.Integer, nullable=False)

class Enrollment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    course_id = db.Column(db.String(36), db.ForeignKey('course.id'), nullable=False)
    last_completed_chapter_number = db.Column(db.Integer, default=0, nullable=False)
    completed_at = db.Column(db.DateTime, nullable=True, default=None)
    user = db.relationship('User', back_populates='enrollments')
    course = db.relationship('Course', back_populates='enrollees')
    chat_histories = db.relationship('ChatHistory', backref='enrollment', lazy='dynamic', cascade="all, delete-orphan")
    __table_args__ = (db.UniqueConstraint('user_id', 'course_id', name='_user_course_uc'),)

class ChatHistory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    enrollment_id = db.Column(db.Integer, db.ForeignKey('enrollment.id'), nullable=False)
    lesson_id = db.Column(db.String(36), db.ForeignKey('lesson.id'), nullable=False)
    history_json = db.Column(db.Text, nullable=False, default='[]')
    current_step_index = db.Column(db.Integer, nullable=False, default=0)
    current_chunk_index = db.Column(db.Integer, nullable=False, default=0)
    __table_args__ = (db.UniqueConstraint('enrollment_id', 'lesson_id', name='_enrollment_lesson_uc'),)

class Review(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    rating = db.Column(db.Integer, nullable=False)
    comment = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.datetime.utcnow)
    course_id = db.Column(db.String(36), db.ForeignKey('course.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    __table_args__ = (db.UniqueConstraint('user_id', 'course_id', name='_user_course_review_uc'),)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# --- Prompts ---
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
GRADER_PROMPT = """
You are an impartial grading assistant. Your task is to determine if a student's answer contains a set of key concepts. Your response MUST be a single word: "CORRECT" or "INCORRECT".
Required Keywords: {}
Student's Answer: {}
"""
TUTOR_PROMPT_TEMPLATE = {
    "CONTENT": """
You are a friendly and engaging tutor. Your task is to teach the following information to a student.
Your goal is to be comprehensive and ensure no details are lost. Explain the provided text clearly, including all examples and specific terms mentioned.
After explaining the content, ask a simple question to prompt the user to continue, like "Does that make sense?" or "Shall we move on?".

Here is the text to explain:
---
{}
---
""",
    "MEDIA_IMAGE": "An image with the description '{}' has just been shown. Briefly call the student's attention to it and ask if they are ready to continue.",
    "MEDIA_AUDIO": "An audio clip with the description '{}' is available to play. Briefly encourage the student to listen to it and ask if they are ready to continue when they're done.",
    "QUESTION": "Okay, time for a quick question to check your understanding: {}"
}

INTENT_CLASSIFIER_PROMPT = """
You are an intent classification agent. Your task is to analyze a user's input during a lesson and determine their intent.
The user's input is: "{}"
The available media descriptions in this lesson are: {}

You MUST respond with a single, specific JSON object. Choose ONE of the following intents:

1.  If the user is asking a general question about the lesson content, respond with:
    {{"intent": "QNA", "query": "the user's original question"}}

2.  If the user is asking to see a specific piece of media again AND their request matches one of the available media descriptions, respond with:
    {{"intent": "MEDIA_REQUEST", "alt_text": "the matching media description from the list"}}

3.  If the user's request is unclear or doesn't fit the above, default to a general question:
    {{"intent": "QNA", "query": "the user's original question"}}

User Input: "{}"
"""
# --- Helper Functions & Decorators ---
def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            flash("You do not have permission to access this page.", "danger")
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated_function

def parse_lesson_script(script_text):
    try:
        model = genai.GenerativeModel('gemini-1.5-pro-latest')
        response = model.generate_content(PARSER_PROMPT + script_text)
        cleaned_response = response.text.strip().replace("```json", "").replace("```", "").strip()
        parsed_json = json.loads(cleaned_response)
        for step in parsed_json.get('steps', []):
            if step.get('type') == 'QUESTION_SA' and 'keywords' in step:
                kw = step['keywords']
                if isinstance(kw, str): kw = [k.strip() for k in kw.split(',')]
                step['keywords'] = [str(k) for k in step['keywords']]
        return parsed_json if isinstance(parsed_json, dict) and 'steps' in parsed_json else None
    except Exception as e:
        print(f"Error during parsing: {e}")
        return None

def get_tutor_response(full_prompt):
    try:
        print("\n" + "="*80)
        print("--- PROMPT SENT TO GEMINI: ---")
        print(full_prompt)
        print("="*80 + "\n")
        model = genai.GenerativeModel('gemini-1.5-pro-latest')
        response = model.generate_content(full_prompt)
        response_text = response.text if response.text else "Let's try that another way."
        print("\n" + "-"*80)
        print("--- RESPONSE RECEIVED FROM GEMINI: ---")
        print(response_text)
        print("-"*80 + "\n")
        return response_text
    except Exception as e:
        print(f"Error getting tutor response: {e}")
        return "I seem to be having a little trouble thinking. Could you try again?"

# --- RAG (Retrieval-Augmented Generation) Helper Functions ---
def _get_or_create_rag_retriever(lesson_id, lesson_script):
    if lesson_id in RAG_RETRIEVERS:
        return RAG_RETRIEVERS[lesson_id]
    
    text_chunks = [chunk for chunk in lesson_script.split('\n\n') if chunk.strip()]
    if not text_chunks: return None

    try:
        embeddings = genai.embed_content(model='models/text-embedding-004', content=text_chunks, task_type="RETRIEVAL_DOCUMENT")['embedding']
        
        # Use a simple list of dictionaries instead of a DataFrame
        rag_data = []
        for i, chunk in enumerate(text_chunks):
            rag_data.append({'text': chunk, 'embedding': embeddings[i]})
        
        RAG_RETRIEVERS[lesson_id] = rag_data
        print(f"RAG retriever created and cached for lesson {lesson_id}.")
        return rag_data
    except Exception as e:
        print(f"Error creating RAG embeddings: {e}")
        return None

def answer_question_with_rag(question, rag_data):
    if not rag_data:
        return "I'm sorry, I don't have enough information to answer that."

    try:
        query_embedding = genai.embed_content(model='models/text-embedding-004', content=question, task_type="RETRIEVAL_QUERY")['embedding']
    except Exception as e:
        print(f"Error embedding RAG query: {e}")
        return "I had trouble understanding your question. Please try rephrasing."

    # Calculate similarity using standard Python and a helper for dot product
    def dot_product(v1, v2):
        return sum(x*y for x, y in zip(v1, v2))

    for item in rag_data:
        item['similarity'] = dot_product(item['embedding'], query_embedding)

    # Sort the list of dictionaries and get the top results
    sorted_data = sorted(rag_data, key=lambda x: x['similarity'], reverse=True)
    top_chunks = [item['text'] for item in sorted_data[:3]]
    
    context = "\n---\n".join(top_chunks)
    
    rag_prompt = f"""
Based ONLY on the following context, provide a concise answer to the user's question. If the context doesn't contain the answer, say "That's a great question, but it's not covered in this chapter's material."

CONTEXT:
{context}

USER'S QUESTION:
{question}
"""
    return get_tutor_response(rag_prompt)


# --- Admin Routes ---
@app.route('/admin/dashboard')
@login_required
@admin_required
def admin_dashboard():
    pending_courses = Course.query.filter_by(status='pending_review').all()
    return render_template('admin_dashboard.html', pending_courses=pending_courses)

@app.route('/admin/course/<string:course_id>/decide', methods=['POST'])
@login_required
@admin_required
def decide_course(course_id):
    course = Course.query.get_or_404(course_id)
    decision = request.form.get('decision')

    if decision == 'approve':
        course.status = 'published'
        course.is_published = True
        flash(f"Course '{course.title}' has been approved and published.", 'success')
    elif decision == 'reject':
        course.status = 'rejected'
        course.is_published = False
        flash(f"Course '{course.title}' has been rejected and returned to the creator.", 'warning')
    
    db.session.commit()
    return redirect(url_for('admin_dashboard'))


# --- Chat and Auth Routes ---
@app.route('/chat/intent', methods=['POST'])
@login_required
def classify_intent():
    data = request.json
    user_input = data.get('user_input')
    lesson_id = data.get('lesson_id')

    lesson = Lesson.query.get_or_404(lesson_id)
    lesson_steps = json.loads(lesson.parsed_json).get('steps', [])
    
    media_descriptions = [
        step.get('alt_text') for step in lesson_steps 
        if step.get('type') == 'MEDIA' and step.get('alt_text')
    ]
    
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
def chat():
    data = request.json
    lesson_id = data.get('lesson_id')
    user_input = data.get('user_input')
    request_type = data.get('request_type', 'LESSON_FLOW')

    lesson = Lesson.query.get_or_404(lesson_id)
    lesson_steps = json.loads(lesson.parsed_json).get('steps', [])

    # --- Intent Handling for Typed User Input ---
    if request_type == 'MEDIA_REQUEST':
        requested_alt_text = user_input
        media_to_show = None
        for step in lesson_steps:
            if step.get('type') == 'MEDIA' and step.get('alt_text') == requested_alt_text:
                media_to_show = step
                break
        
        if media_to_show:
            response_data = {
                "tutor_text": f"Of course, here is '{media_to_show.get('alt_text')}' again.",
                "media_url": media_to_show.get('media_url'),
                "media_type": media_to_show.get('media_type'),
                "is_qna_response": True
            }
            return jsonify(response_data)
        else:
            # If media not found, treat it as a regular question
            request_type = 'QNA'

    # --- Authorization and State Loading ---
    enrollment = Enrollment.query.filter_by(user_id=current_user.id, course_id=lesson.course_id).first()
    is_creator = (current_user.id == lesson.course.user_id)

    # 1. Check authorization: must be enrolled, the creator, or an admin
    if not enrollment and not is_creator and not current_user.is_admin:
        abort(403, "Forbidden")

    # 2. Load state: progress and conversation log
    history_record = None
    step_index, chunk_index = 0, 0
    chat_log = []
    session_key = f'preview_chat_{lesson_id}'

    if enrollment:
        # Enrolled student: use the database for persistence
        history_record = ChatHistory.query.filter_by(enrollment_id=enrollment.id, lesson_id=lesson.id).first()
        if history_record:
            step_index = history_record.current_step_index
            chunk_index = history_record.current_chunk_index
            chat_log = json.loads(history_record.history_json)
        else:
            # First time in this chapter, create a new record
            history_record = ChatHistory(enrollment_id=enrollment.id, lesson_id=lesson.id)
            db.session.add(history_record)
    else: 
        # Creator or Admin previewing: use the temporary session
        if session_key in session and user_input is not None:
             chat_log = session.get(session_key, {}).get('chat_log', [])
             step_index = session.get(session_key, {}).get('step_index', 0)
             chunk_index = session.get(session_key, {}).get('chunk_index', 0)
        else:
             # Start a fresh preview session
             session[session_key] = {'step_index': 0, 'chunk_index': 0, 'chat_log': []}

    # --- Log User Input (if applicable) ---
    if user_input and request_type == 'LESSON_FLOW' and user_input != 'Continue':
        chat_log.append({"sender": "student", "type": "text", "content": user_input})
    
    # --- Q&A Path (interrupts normal flow) ---
    if request_type == 'QNA':
        chat_log.append({"sender": "student", "type": "text", "content": user_input})
        retriever = _get_or_create_rag_retriever(lesson.id, lesson.raw_script)
        response_text = answer_question_with_rag(user_input, retriever)
        chat_log.append({"sender": "tutor", "type": "text", "content": response_text})
        
        # Save the Q&A to the log immediately
        if history_record: 
            history_record.history_json = json.dumps(chat_log)
        else: # Save to session for previews
             session[session_key]['chat_log'] = chat_log
        db.session.commit()
        
        return jsonify({'is_qna_response': True, 'tutor_text': response_text})

    # --- Main Lesson Flow (State Machine) ---
    response_data = {}
    model_response_text = ""
    next_step_index, next_chunk_index = step_index, chunk_index

    if step_index >= len(lesson_steps):
        response_data['is_lesson_end'] = True
        model_response_text = "Congratulations! You've completed this chapter."
        if enrollment and enrollment.last_completed_chapter_number < lesson.chapter_number:
            enrollment.last_completed_chapter_number = lesson.chapter_number
            if enrollment.last_completed_chapter_number >= len(lesson.course.lessons):
                if not enrollment.completed_at:
                    enrollment.completed_at = datetime.datetime.utcnow()
                response_data['certificate_url'] = url_for('certificate_view', course_id=lesson.course_id)
            else:
                next_chapter_num = lesson.chapter_number + 1
                response_data['next_chapter_url'] = url_for(
                    'student_chapter_view', 
                    course_id=lesson.course_id, 
                    chapter_number=next_chapter_num
                )
    else:
        current_step = lesson_steps[step_index]
        step_type = current_step.get('type')

        if step_type == 'CONTENT':
            content_chunks = [chunk for chunk in current_step.get('text', '').split('\n\n') if chunk.strip()]
            if chunk_index < len(content_chunks):
                chunk_to_explain = content_chunks[chunk_index]
                prompt = TUTOR_PROMPT_TEMPLATE['CONTENT'].format(chunk_to_explain)
                model_response_text = get_tutor_response(prompt)
                chat_log.append({"sender": "tutor", "type": "text", "content": model_response_text})
                next_chunk_index = chunk_index + 1
            if next_chunk_index >= len(content_chunks):
                next_step_index = step_index + 1
                next_chunk_index = 0
        else:  # Handles MEDIA, QUESTION_MCQ, QUESTION_SA
            if step_type == 'MEDIA':
                media_type = current_step.get('media_type', 'image')
                prompt_template = TUTOR_PROMPT_TEMPLATE.get(f"MEDIA_{media_type.upper()}", TUTOR_PROMPT_TEMPLATE['MEDIA_IMAGE'])
                prompt = prompt_template.format(current_step.get('alt_text', ''))
                
                model_response_text = get_tutor_response(prompt)
                chat_log.append({"sender": "tutor", "type": "text", "content": model_response_text})
                chat_log.append({
                    "sender": "tutor", 
                    "type": media_type, 
                    "url": current_step.get('media_url'), 
                    "alt": current_step.get('alt_text')
                })
                response_data['media_url'] = current_step.get('media_url')
                response_data['media_type'] = media_type

            elif step_type in ['QUESTION_MCQ', 'QUESTION_SA']:
                prompt = TUTOR_PROMPT_TEMPLATE['QUESTION'].format(current_step.get('question', ''))
                model_response_text = get_tutor_response(prompt)
                chat_log.append({"sender": "tutor", "type": "text", "content": model_response_text})
                response_data['question'] = current_step
            
            next_step_index = step_index + 1
            next_chunk_index = 0

    if model_response_text:
        response_data['tutor_text'] = model_response_text

    # --- State Saving ---
    if enrollment:
        history_record.current_step_index = next_step_index
        history_record.current_chunk_index = next_chunk_index
        history_record.history_json = json.dumps(chat_log)
    else:
        session[session_key] = {
            'step_index': next_step_index, 
            'chunk_index': next_chunk_index,
            'chat_log': chat_log
        }
    
    db.session.commit()
    return jsonify(response_data)

@app.route('/chat/reset', methods=['POST'])
@login_required
def reset_conversation():
    lesson_id = request.json.get('lesson_id')
    lesson = Lesson.query.get_or_404(lesson_id)
    enrollment = Enrollment.query.filter_by(user_id=current_user.id, course_id=lesson.course_id).first()
    if enrollment:
        history_record = ChatHistory.query.filter_by(enrollment_id=enrollment.id, lesson_id=lesson.id).first()
        if history_record:
            history_record.current_step_index = 0
            history_record.current_chunk_index = 0
            history_record.history_json = '[]'
            db.session.commit()
    else: 
        session_key = f'preview_chat_{lesson_id}'
        if session_key in session:
            del session[session_key]
    return jsonify({'success': True})

@app.route('/chat/delete_last_turn', methods=['POST'])
@login_required
def delete_last_turn():
    lesson_id = request.json.get('lesson_id')
    lesson = Lesson.query.get_or_404(lesson_id)
    enrollment = Enrollment.query.filter_by(user_id=current_user.id, course_id=lesson.course_id).first()
    if enrollment:
        history_record = ChatHistory.query.filter_by(enrollment_id=enrollment.id, lesson_id=lesson.id).first()
        if history_record:
            if history_record.current_chunk_index > 0:
                history_record.current_chunk_index -= 1
            elif history_record.current_step_index > 0:
                history_record.current_step_index -= 1
                history_record.current_chunk_index = 0 
            db.session.commit()
    else:
        session_key = f'preview_chat_{lesson_id}'
        if session_key in session:
            if session[session_key]['chunk_index'] > 0:
                session[session_key]['chunk_index'] -= 1
            elif session[session_key]['step_index'] > 0:
                session[session_key]['step_index'] -= 1
                session[session_key]['chunk_index'] = 0
    return jsonify({'success': True})

@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated: return redirect(url_for('dashboard'))
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        if User.query.filter_by(username=username).first():
            flash('Username already exists.', 'warning')
            return redirect(url_for('register'))
        new_user = User(username=username)
        new_user.set_password(password)
        db.session.add(new_user)
        db.session.commit()
        flash('Account created successfully! Please log in.', 'success')
        return redirect(url_for('login'))
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated: return redirect(url_for('dashboard'))
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        user = User.query.filter_by(username=username).first()
        if user is None or not user.check_password(password):
            flash('Invalid username or password.', 'danger')
            return redirect(url_for('login'))
        login_user(user, remember=True)
        next_page = request.args.get('next')
        return redirect(next_page or url_for('dashboard'))
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('You have been logged out.', 'info')
    return redirect(url_for('login'))

@app.route('/')
def index():
    return redirect(url_for('explore'))

@app.route('/dashboard')
@login_required
def dashboard():
    enrollments = Enrollment.query.filter_by(user_id=current_user.id).join(Course).order_by(Course.title).all()
    return render_template('dashboard.html', enrollments=enrollments)

@app.route('/creator')
@login_required
def creator_dashboard():
    created_courses = Course.query.filter_by(user_id=current_user.id).order_by(Course.title).all()
    return render_template('creator_dashboard.html', created_courses=created_courses)

@app.route('/course/create', methods=['POST'])
@login_required
def create_course():
    title = request.form.get('title')
    if not title:
        flash('A title is required to create a course.', 'warning')
        return redirect(url_for('creator_dashboard'))
    new_course = Course(title=title, user_id=current_user.id)
    db.session.add(new_course)
    db.session.commit()
    flash('Course created! You can now manage it.', 'success')
    return redirect(url_for('manage_course', course_id=new_course.id))

@app.route('/course/<string:course_id>/manage')
@login_required
def manage_course(course_id):
    course = Course.query.get_or_404(course_id)
    if course.creator.id != current_user.id: abort(403)
    return render_template('manage_course.html', course=course)

@app.route('/course/<string:course_id>/add_chapter', methods=['GET'])
@login_required
def add_chapter_page(course_id):
    course = Course.query.get_or_404(course_id)
    if course.creator.id != current_user.id: abort(403)
    return render_template('create_chapter.html', course=course)

@app.route('/course/<string:course_id>/save_chapter', methods=['POST'])
@login_required
def save_chapter(course_id):
    course = Course.query.get_or_404(course_id)
    if course.creator.id != current_user.id: abort(403)
    script = request.form['script']
    title = request.form['title']
    if not title or not script:
        flash('Both a title and script are required.', 'warning')
        return redirect(url_for('add_chapter_page', course_id=course.id))
    
    uploaded_files = request.files.getlist('media_files')
    image_urls = []
    audio_urls = []
    for uploaded_file in uploaded_files:
        if uploaded_file.filename != '':
            if not allowed_file(uploaded_file.filename):
                flash('Invalid file type. Allowed types are: png, jpg, jpeg, gif, mp3, wav, ogg', 'danger')
                return redirect(request.url)
            filename = str(uuid.uuid4()) + os.path.splitext(uploaded_file.filename)[1]
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            uploaded_file.save(filepath)
            url = url_for('static', filename=f'uploads/{filename}')
            if uploaded_file.content_type.startswith('image/'):
                image_urls.append(url)
            elif uploaded_file.content_type.startswith('audio/'):
                audio_urls.append(url)

    parsed_data = parse_lesson_script(script)
    if not parsed_data:
        flash('The AI could not understand the lesson structure. Please check your tags and try again.', 'danger')
        return redirect(url_for('add_chapter_page', course_id=course.id))
    
    image_url_iterator = iter(image_urls)
    audio_url_iterator = iter(audio_urls)
    
    for step in parsed_data.get('steps', []):
        if step.get('type') == 'MEDIA':
            if step.get('media_type') == 'image':
                try: step['media_url'] = next(image_url_iterator)
                except StopIteration: step['media_url'] = None
            elif step.get('media_type') == 'audio':
                try: step['media_url'] = next(audio_url_iterator)
                except StopIteration: step['media_url'] = None
            
    last_chapter = Lesson.query.filter_by(course_id=course.id).order_by(Lesson.chapter_number.desc()).first()
    new_chapter_number = (last_chapter.chapter_number + 1) if last_chapter else 1
    new_lesson = Lesson(title=title, raw_script=script, parsed_json=json.dumps(parsed_data), course_id=course.id, chapter_number=new_chapter_number)
    db.session.add(new_lesson)
    db.session.commit()
    flash('Chapter added successfully!', 'success')
    return redirect(url_for('manage_course', course_id=course.id))

@app.route('/chapter/<string:lesson_id>/edit', methods=['GET'])
@login_required
def edit_chapter_page(lesson_id):
    lesson = Lesson.query.get_or_404(lesson_id)
    if lesson.course.creator.id != current_user.id: abort(403)
    return render_template('edit_chapter.html', lesson=lesson)

@app.route('/chapter/<string:lesson_id>/update', methods=['POST'])
@login_required
def update_chapter(lesson_id):
    lesson = Lesson.query.get_or_404(lesson_id)
    if lesson.course.creator.id != current_user.id: abort(403)
    
    old_media_map = {}
    if lesson.parsed_json:
        try:
            old_data = json.loads(lesson.parsed_json)
            for step in old_data.get('steps', []):
                if step.get('type') == 'MEDIA' and step.get('media_url'):
                    old_media_map[step.get('alt_text')] = step['media_url']
        except (json.JSONDecodeError, AttributeError): pass

    uploaded_files = request.files.getlist('media_files')
    image_urls = []
    audio_urls = []
    for uploaded_file in uploaded_files:
        if uploaded_file.filename != '':
            if not allowed_file(uploaded_file.filename):
                flash('Invalid file type. Allowed types are: png, jpg, jpeg, gif, mp3, wav, ogg', 'danger')
                return redirect(request.url)
            filename = str(uuid.uuid4()) + os.path.splitext(uploaded_file.filename)[1]
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            uploaded_file.save(filepath)
            url = url_for('static', filename=f'uploads/{filename}')
            if uploaded_file.content_type.startswith('image/'):
                image_urls.append(url)
            elif uploaded_file.content_type.startswith('audio/'):
                audio_urls.append(url)

    lesson.title = request.form['title']
    lesson.raw_script = request.form['script']
    
    parsed_data = parse_lesson_script(lesson.raw_script)
    if not parsed_data:
        flash('The AI could not understand the lesson structure.', 'danger')
        return redirect(url_for('edit_chapter_page', lesson_id=lesson.id))
        
    image_url_iterator = iter(image_urls)
    audio_url_iterator = iter(audio_urls)

    for step in parsed_data.get('steps', []):
        if step.get('type') == 'MEDIA':
            media_type = step.get('media_type')
            alt_text = step.get('alt_text')
            assigned_url = old_media_map.get(alt_text) 

            if media_type == 'image':
                try: assigned_url = next(image_url_iterator)
                except StopIteration: pass
            elif media_type == 'audio':
                try: assigned_url = next(audio_url_iterator)
                except StopIteration: pass
            
            step['media_url'] = assigned_url

    lesson.parsed_json = json.dumps(parsed_data)
    if lesson.id in RAG_RETRIEVERS: del RAG_RETRIEVERS[lesson.id]
    db.session.commit()
    flash('Chapter updated successfully!', 'success')
    return redirect(url_for('manage_course', course_id=lesson.course.id))

@app.route('/chapter/<string:lesson_id>/delete', methods=['POST'])
@login_required
def delete_chapter(lesson_id):
    lesson = Lesson.query.get_or_404(lesson_id)
    if lesson.course.creator.id != current_user.id: abort(403)
    course_id = lesson.course_id
    deleted_chapter_number = lesson.chapter_number
    db.session.delete(lesson)
    subsequent_chapters = Lesson.query.filter(Lesson.course_id == course_id, Lesson.chapter_number > deleted_chapter_number).order_by(Lesson.chapter_number).all()
    for chapter in subsequent_chapters: chapter.chapter_number -= 1
    if lesson_id in RAG_RETRIEVERS: del RAG_RETRIEVERS[lesson.id]
    db.session.commit()
    flash('Chapter deleted successfully.', 'success')
    return redirect(url_for('manage_course', course_id=course.id))

@app.route('/course/<string:course_id>/submit_review', methods=['POST'])
@login_required
def submit_for_review(course_id):
    course = Course.query.get_or_404(course_id)
    if course.creator.id != current_user.id: abort(403)
    
    course.status = 'pending_review'
    course.is_published = False
    db.session.commit()
    
    flash('Your course has been submitted for review!', 'success')
    return redirect(url_for('manage_course', course_id=course.id))

@app.route('/course/<string:course_id>/unpublish', methods=['POST'])
@login_required
def unpublish_course(course_id):
    course = Course.query.get_or_404(course_id)
    if course.creator.id != current_user.id and not current_user.is_admin:
        abort(403)
        
    course.status = 'draft'
    course.is_published = False
    db.session.commit()
    
    flash('Course has been unpublished and returned to draft status.', 'info')
    return redirect(url_for('manage_course', course_id=course.id))

@app.route('/explore')
def explore():
    courses = Course.query.filter_by(status='published').order_by(Course.title).all()
    return render_template('explore.html', courses=courses)

@app.route('/course/<string:course_id>')
@login_required
def course_player(course_id):
    course = Course.query.get_or_404(course_id)
    is_authorized = course.status == 'published' or \
                    (current_user.is_authenticated and (
                        current_user.id == course.user_id or 
                        current_user.is_enrolled(course) or 
                        current_user.is_admin
                    ))
    if not is_authorized: abort(404)
    if not course.lessons:
        if current_user.is_authenticated and current_user.id == course.user_id:
            flash('This course has no chapters yet. Add one to enable the preview.', 'info')
            return redirect(url_for('manage_course', course_id=course.id))
        flash("This course has no content yet.", "warning")
        return redirect(url_for('explore'))
    chapter_to_start = 1
    enrollment = Enrollment.query.filter_by(user_id=current_user.id, course_id=course.id).first()
    if enrollment:
        chapter_to_start = enrollment.last_completed_chapter_number + 1
        if chapter_to_start > len(course.lessons): chapter_to_start = len(course.lessons)
    return redirect(url_for('student_chapter_view', course_id=course.id, chapter_number=chapter_to_start))

@app.route('/course/<string:course_id>/<int:chapter_number>')
@login_required
def student_chapter_view(course_id, chapter_number):
    course = Course.query.get_or_404(course_id)
    is_authorized = course.status == 'published' or \
                    (current_user.is_authenticated and (
                        current_user.id == course.user_id or 
                        current_user.is_enrolled(course) or 
                        current_user.is_admin
                    ))
    if not is_authorized: abort(404)
    lesson = Lesson.query.filter_by(course_id=course.id, chapter_number=chapter_number).first_or_404()
    enrollment = Enrollment.query.filter_by(user_id=current_user.id, course_id=course.id).first()
    
    initial_history_data = None
    if enrollment:
        chat_history_record = ChatHistory.query.filter_by(enrollment_id=enrollment.id, lesson_id=lesson.id).first()
        if chat_history_record:
            initial_history_data = {
                "history_json": chat_history_record.history_json, 
                "current_step_index": chat_history_record.current_step_index, 
                "current_chunk_index": chat_history_record.current_chunk_index
            }
    else: 
        session_key = f'preview_chat_{lesson.id}'
        if session_key in session:
            del session[session_key]
            
    return render_template('course_player.html', course=course, current_lesson=lesson, enrollment=enrollment, initial_history=initial_history_data)

@app.route('/course/<string:course_id>/enroll', methods=['POST'])
@login_required
def enroll_in_course(course_id):
    course = Course.query.get_or_404(course_id)
    if current_user.id == course.user_id:
        flash("You cannot enroll in a course you've created.", "warning")
        return redirect(url_for('course_detail_page', course_id=course.id))
    if current_user.is_enrolled(course):
        flash("You are already enrolled in this course.", "info")
        return redirect(url_for('course_player', course_id=course.id))
    new_enrollment = Enrollment(user_id=current_user.id, course_id=course.id)
    db.session.add(new_enrollment)
    db.session.commit()
    flash(f"You have successfully enrolled in '{course.title}'!", 'success')
    return redirect(url_for('course_player', course_id=course.id))

@app.route('/course/<string:course_id>/details')
def course_detail_page(course_id):
    course = Course.query.get_or_404(course_id)
    share_id = request.args.get('share_id')
    is_authorized = course.status == 'published' or \
                    (current_user.is_authenticated and (
                        current_user.id == course.user_id or 
                        current_user.is_admin
                    )) or \
                    (course.shareable_link_id and course.shareable_link_id == share_id)
    if not is_authorized: abort(404)
    return render_template('course_detail.html', course=course, share_id=share_id)

@app.route('/course/<string:course_id>/reviews')
def reviews_page(course_id):
    course = Course.query.get_or_404(course_id)
    reviews = course.reviews.order_by(Review.created_at.desc()).all()
    return render_template('reviews.html', course=course, reviews=reviews)

@app.route('/course/<string:course_id>/review', methods=['POST'])
@login_required
def submit_review(course_id):
    course = Course.query.get_or_404(course_id)
    enrollment = Enrollment.query.filter_by(user_id=current_user.id, course_id=course_id).first()
    if not enrollment or not enrollment.completed_at:
        flash("You must complete a course before reviewing it.", "warning")
        return redirect(url_for('explore'))
    if Review.query.filter_by(user_id=current_user.id, course_id=course_id).first():
        flash("You have already reviewed this course.", "warning")
        return redirect(url_for('reviews_page', course_id=course.id))
    rating = request.form.get('rating')
    comment = request.form.get('comment')
    if not rating:
        flash("A star rating is required.", "warning")
        return redirect(url_for('certificate_view', course_id=course.id))
    new_review = Review(rating=int(rating), comment=comment, course_id=course.id, user_id=current_user.id)
    db.session.add(new_review)
    db.session.commit()
    flash("Thank you for your feedback!", "success")
    return redirect(url_for('reviews_page', course_id=course.id))

@app.route('/course/<string:course_id>/certificate')
@login_required
def certificate_view(course_id):
    enrollment = Enrollment.query.filter_by(user_id=current_user.id, course_id=course_id).first_or_404()
    if not enrollment.completed_at:
        flash("You have not completed this course yet.", "warning")
        return redirect(url_for('course_player', course_id=course.id))
    existing_review = Review.query.filter_by(user_id=current_user.id, course_id=course.id).first()
    return render_template('certificate.html', enrollment=enrollment, existing_review=existing_review)

@app.route('/course/<string:course_id>/update_details', methods=['POST'])
@login_required
def update_course_details(course_id):
    course = Course.query.get_or_404(course_id)
    if course.creator.id != current_user.id: abort(403)
    course.description = request.form.get('description')
    if 'thumbnail' in request.files:
        file = request.files['thumbnail']
        if file.filename != '':
            if not allowed_file(file.filename):
                flash('Invalid file type for thumbnail. Allowed types are: png, jpg, jpeg, gif', 'danger')
                return redirect(request.url)
            filename = str(uuid.uuid4()) + os.path.splitext(file.filename)[1]
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(filepath)
            course.thumbnail_url = url_for('static', filename=f'uploads/{filename}')
    db.session.commit()
    flash('Course details updated successfully!', 'success')
    return redirect(url_for('manage_course', course_id=course.id))

@app.route('/course/<string:course_id>/generate_link', methods=['POST'])
@login_required
def generate_share_link(course_id):
    course = Course.query.get_or_404(course_id)
    if course.creator.id != current_user.id: abort(403)
    if not course.shareable_link_id:
        course.shareable_link_id = str(uuid.uuid4())
        db.session.commit()
    return redirect(url_for('manage_course', course_id=course.id))

@app.route('/share/<string:link_id>')
def shared_course_view(link_id):
    course = Course.query.filter_by(shareable_link_id=link_id).first_or_404()
    return render_template('course_detail.html', course=course, share_id=link_id)

if __name__ == '__main__':
    app.run(debug=True)