import os
import json
import logging
import re
from datetime import datetime, date, timedelta
from typing import List, Optional

from flask import Flask, request, jsonify, send_from_directory, render_template
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy import Integer, String, Text, ForeignKey, Date, Boolean, Float
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import SecretStr, Field
from google import genai
from google.genai import types

# ==========================================
# PART 1: CONFIGURATION
# ==========================================

# Configure logging to show up in the terminal
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class AppSettings(BaseSettings):
    gemini_api_key: SecretStr = Field(..., alias='GEMINI_API_KEY')
    heavy_model: str = Field("gemini-2.5-flash")
    light_model: str = Field("gemini-2.5-flash")
    secret_key: str = Field("dev-secret-key", alias='FLASK_SECRET_KEY')
    database_uri: str = Field("sqlite:///projects_v2.db", alias='DATABASE_URL')
    port: int = Field(5005, description="Port to run the app on")

    model_config = SettingsConfigDict(env_file='.env', extra='ignore')

try:
    settings = AppSettings()
    # Explicit print to confirm API key is loaded (masked)
    print(f"DEBUG: Config loaded. API Key present: {len(settings.gemini_api_key.get_secret_value()) > 0}")
except Exception as e:
    logger.error(f"Config Error: {e}")
    # Allow running without .env for testing DB/UI, but LLM will fail
    pass

app = Flask(__name__)
app.config['SECRET_KEY'] = settings.secret_key
app.config['SQLALCHEMY_DATABASE_URI'] = settings.database_uri
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# === Number of item on dashboard  ===
DASHBOARD_LIMIT = 3 

# Books Configuration
BOOKS_FOLDER = os.path.join(os.getcwd(), 'books')
os.makedirs(BOOKS_FOLDER, exist_ok=True)

db = SQLAlchemy(app)

# Initialize GenAI Client
try:
    client = genai.Client(api_key=settings.gemini_api_key.get_secret_value())
    print("DEBUG: Google GenAI Client Initialized successfully.")
except Exception as e:
    print(f"ERROR: Failed to initialize GenAI Client: {e}")
    client = None

# ==========================================
# PART 2: DATABASE MODELS
# ==========================================

class Project(db.Model):
    __tablename__ = 'projects'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    description_raw: Mapped[str] = mapped_column(Text, nullable=True)
    color: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[date] = mapped_column(Date, default=date.today)
    is_completed: Mapped[bool] = mapped_column(Boolean, default=False)
    
    steps: Mapped[List["ProjectStep"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    logs: Mapped[List["DailyLog"]] = relationship(back_populates="project")
    questions: Mapped[List["Question"]] = relationship(back_populates="project", cascade="all, delete-orphan")

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "color": self.color,
            "description": self.description_raw,
            "is_completed": self.is_completed,
            "steps": [s.to_dict() for s in self.steps]
        }

class ProjectStep(db.Model):
    __tablename__ = 'project_steps'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey('projects.id'), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    step_order: Mapped[int] = mapped_column(Integer, nullable=False)
    is_completed: Mapped[bool] = mapped_column(Boolean, default=False)
    
    project: Mapped["Project"] = relationship(back_populates="steps")
    logs: Mapped[List["DailyLog"]] = relationship(back_populates="step")
    questions: Mapped[List["Question"]] = relationship(back_populates="step", cascade="all, delete-orphan")

    def to_dict(self):
        return {
            "id": self.id,
            "content": self.content,
            "order": self.step_order,
            "is_completed": self.is_completed
        }

class DailyLog(db.Model):
    __tablename__ = 'daily_logs'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    date_val: Mapped[date] = mapped_column(Date, nullable=False)
    project_id: Mapped[int] = mapped_column(ForeignKey('projects.id'), nullable=False)
    step_id: Mapped[Optional[int]] = mapped_column(ForeignKey('project_steps.id'), nullable=True)
    
    url_external: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    book_filename: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    book_page: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    
    raw_notes: Mapped[str] = mapped_column(Text, nullable=True)
    organized_notes: Mapped[str] = mapped_column(Text, nullable=True)
    
    project: Mapped["Project"] = relationship(back_populates="logs")
    step: Mapped["ProjectStep"] = relationship(back_populates="logs")

    def to_dict(self):
        effective_url = self.url_external
        source_label = "Web"
        if self.book_filename:
            effective_url = f"/books/{self.book_filename}#page={self.book_page}"
            source_label = f"Book: {self.book_filename} (p.{self.book_page})"
        
        return {
            "id": self.id,
            "date": self.date_val.isoformat(),
            "project_id": self.project_id,
            "step_id": self.step_id,
            "project_name": self.project.name,
            "color": self.project.color,
            "step_content": self.step.content if self.step else None,
            "url": effective_url,
            "raw_url": self.url_external, # For editing form
            "book_filename": self.book_filename,
            "book_page": self.book_page,
            "source_label": source_label,
            "notes": self.organized_notes or self.raw_notes
        }

class Question(db.Model):
    __tablename__ = 'questions'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey('projects.id'), nullable=False)
    step_id: Mapped[Optional[int]] = mapped_column(ForeignKey('project_steps.id'), nullable=True)
    
    question_text: Mapped[str] = mapped_column(Text, nullable=False)
    option_a: Mapped[str] = mapped_column(String(255), nullable=False)
    option_b: Mapped[str] = mapped_column(String(255), nullable=False)
    option_c: Mapped[str] = mapped_column(String(255), nullable=False)
    option_d: Mapped[str] = mapped_column(String(255), nullable=False)
    correct_answer: Mapped[str] = mapped_column(String(10), nullable=False)
    explanation: Mapped[str] = mapped_column(Text, nullable=False)
    
    # SM-2 Algorithm Fields
    ease_factor: Mapped[float] = mapped_column(Float, default=2.5)
    interval: Mapped[int] = mapped_column(Integer, default=0)
    repetition_count: Mapped[int] = mapped_column(Integer, default=0)
    next_review_date: Mapped[date] = mapped_column(Date, default=date.today)
    
    project: Mapped["Project"] = relationship(back_populates="questions")
    step: Mapped["ProjectStep"] = relationship(back_populates="questions")

    def to_dict(self):
        return {
            "id": self.id,
            "question": self.question_text,
            "options": {"A": self.option_a, "B": self.option_b, "C": self.option_c, "D": self.option_d},
            "correct_answer": self.correct_answer,
            "explanation": self.explanation,
            "next_review": self.next_review_date.isoformat(),
            "project_color": self.project.color if self.project else "#0d6efd"
        }
# ==========================================
# PART 3: LLM HELPERS & LOGIC
# ==========================================

SPACED_REPETITION_PROMPT = """
You are an expert educational content generator. Analyze the text provided and generate 3 to 5 multiple-choice questions for spaced repetition.

IMPORTANT MATH FORMATTING:
- Use LaTeX for ALL mathematical expressions, equations, variables, and numbers.
- Enclose all LaTeX in single dollar signs. Example: "What is $\\frac{a}{b}$?" or "If $x^2 = 4$..."
- Do NOT use plain text for math (e.g., do not write "x^2", write "$x^2$").

Output MUST be a valid JSON array of objects with these keys:
1. "question_text": String.
2. "options": Object with keys "A", "B", "C", "D".
3. "correct_answer": String (e.g. "A").
4. "explanation": String.

SOURCE TEXT:
"""

def parse_generated_questions(raw_text: str) -> List[dict]:
    questions = []
    raw_text = raw_text.replace('\r\n', '\n')
    cards = re.split(r'\n\s*\n', raw_text.strip())
    
    for card in cards:
        if '?' not in card: continue
        parts = card.split('?')
        if len(parts) < 2: continue
        
        front, back = parts[0].strip(), parts[1].strip()
        
        lines = front.split('\n')
        q_text_lines, options = [], {}
        for line in lines:
            m = re.match(r'([A-D])\.\s+(.+)', line.strip())
            if m: options[m.group(1)] = m.group(2).strip()
            else: q_text_lines.append(line)
        
        back_lines = back.split('\n')
        ans_line = back_lines[0].strip()
        explanation = "\n".join(back_lines[1:]).strip()
        m_ans = re.match(r'^([A-D])\.', ans_line)
        
        if len(options) == 4 and m_ans:
            questions.append({
                "question_text": "\n".join(q_text_lines).strip(),
                "option_a": options.get('A'), "option_b": options.get('B'),
                "option_c": options.get('C'), "option_d": options.get('D'),
                "correct_answer": m_ans.group(1),
                "explanation": explanation
            })
    return questions

def llm_generate_questions(text: str) -> List[dict]:
    if not client: 
        print("ERROR: GenAI Client not initialized.")
        return []
    
    try:
        print("DEBUG: Sending request to LLM (Generate Questions)...")
        
        full_prompt = SPACED_REPETITION_PROMPT + "\n" + text

        # Request JSON mode
        response = client.models.generate_content(
            model=settings.heavy_model,
            contents=full_prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json" 
            )
        )
        print("DEBUG: LLM Response received.")
        
        # 1. Parse JSON
        try:
            raw_data = json.loads(response.text)
        except json.JSONDecodeError:
            clean_text = response.text.strip().replace("```json", "").replace("```", "")
            raw_data = json.loads(clean_text)

        # 2. Handle Wrapping
        if isinstance(raw_data, dict):
            raw_list = raw_data.get("questions") or raw_data.get("items") or []
        elif isinstance(raw_data, list):
            raw_list = raw_data
        else:
            raw_list = []

        # 3. Normalize Data
        cleaned_questions = []
        for item in raw_list:
            q_text = item.get("question_text") or item.get("question")
            opts = item.get("options", {})
            
            # Handle "B. Answer" vs "B"
            raw_ans = item.get("correct_answer") or item.get("answer") or ""
            clean_ans = raw_ans.split('.')[0].strip().upper() if raw_ans else ""
            if len(clean_ans) > 1: clean_ans = clean_ans[0] 

            if q_text and opts and clean_ans:
                cleaned_questions.append({
                    "question_text": q_text,
                    "options": opts,
                    "correct_answer": clean_ans,
                    "explanation": item.get("explanation", "")
                })

        return cleaned_questions

    except Exception as e:
        print(f"ERROR LLM Questions: {e}")
        import traceback
        traceback.print_exc()
        return []

def llm_generate_plan(description: str) -> List[str]:
    if not client: return ["Error: No LLM Client"]
    
    prompt = f"""
    You are a project planner. Convert the following text into a sequential list of project steps.
    
    STRICT RULES:
    1. STRICTLY PRESERVE THE ORDER of the source text. (e.g., Chapter 1 must come before Chapter 2).
    2. Do NOT reorder, prioritize, or group items by topic.
    3. Use Noun Phrases only (remove starting verbs like "Learn", "Read", "Do").
    4. Output MUST be a valid JSON list of strings.

    SOURCE TEXT:
    {description}
    """
    
    try:
        print("DEBUG: Sending request to LLM (Plan)...")
        response = client.models.generate_content(
            model=settings.heavy_model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json"
            )
        )
        return json.loads(response.text)
    except Exception as e:
        print(f"ERROR LLM Plan: {e}")
        return ["Manual Step 1", "Manual Step 2"]
        
        

def llm_organize_notes(raw_text: str) -> str:
    if not client: return raw_text
    prompt = "Format these notes into concise bullet points:\n\n" + raw_text
    try:
        print("DEBUG: Sending request to LLM (Organize)...")
        response = client.models.generate_content(model=settings.light_model, contents=prompt)
        return response.text
    except Exception as e:
        print(f"ERROR LLM Organize: {e}")
        return raw_text
# ==========================================
# PART 4: FLASK ROUTES
# ==========================================

@app.route('/')
def index():
    return render_template('index.html', year=datetime.now().year)

@app.route('/books/<path:filename>')
def serve_book(filename):
    return send_from_directory(BOOKS_FOLDER, filename)

@app.route('/api/books', methods=['GET'])
def list_books():
    files = [f for f in os.listdir(BOOKS_FOLDER) if f.lower().endswith('.pdf')]
    return jsonify(files)

@app.route('/api/projects', methods=['GET', 'POST'])
def handle_projects():
    if request.method == 'GET':
        projects = db.session.execute(db.select(Project)).scalars().all()
        return jsonify([p.to_dict() for p in projects])
    
    if request.method == 'POST':
        data = request.json
        try:
            new_project = Project(
                name=data['name'],
                color=data['color'],
                description_raw=data.get('description', '')
            )
            db.session.add(new_project)
            db.session.flush()
            
            for idx, content in enumerate(data.get('steps', [])):
                db.session.add(ProjectStep(
                    project_id=new_project.id, content=content, step_order=idx
                ))
            
            db.session.commit()
            return jsonify({"status": "success", "id": new_project.id}), 201
        except Exception as e:
            db.session.rollback()
            return jsonify({"error": str(e)}), 400

@app.route('/api/logs/<int:year>', methods=['GET'])
def get_logs(year):
    start, end = date(year, 1, 1), date(year, 12, 31)
    logs = db.session.execute(
        db.select(DailyLog).where(DailyLog.date_val.between(start, end))
    ).scalars().all()
    return jsonify([l.to_dict() for l in logs])

@app.route('/api/logs', methods=['POST'])
def add_or_update_log():
    data = request.json
    try:
        log_date = datetime.strptime(data['date'], "%Y-%m-%d").date()
    except ValueError:
        return jsonify({"error": "Invalid date format"}), 400

    pid = int(data['project_id'])
    log_id = data.get('log_id')
    
    if log_id:
        # Edit existing
        log = db.session.get(DailyLog, int(log_id))
        if not log: return jsonify({"error": "Log not found"}), 404
        log.date_val = log_date
        log.project_id = pid
    else:
        # Create new
        day_logs = db.session.query(DailyLog).filter_by(date_val=log_date).all()
        if len(day_logs) >= 2: return jsonify({"error": "Max 2 projects per day limit reached."}), 400
        if any(l.project_id == pid for l in day_logs): return jsonify({"error": "Project already logged today."}), 400
        log = DailyLog(date_val=log_date, project_id=pid)
        db.session.add(log)

    log.step_id = int(data['step_id']) if data.get('step_id') else None
    
    # URL Logic
    raw_url = data.get('url_external', '').strip()
    if raw_url and not raw_url.startswith(('http://', 'https://')):
        raw_url = 'https://' + raw_url
    log.url_external = raw_url if raw_url else None
    
    log.book_filename = data.get('book_filename')
    log.book_page = int(data['book_page']) if data.get('book_page') else None
    log.raw_notes = data.get('notes', '')
    
    log.organized_notes = data.get('notes', '') 

    if not log.url_external and not log.book_filename:
        return jsonify({"error": "Must provide URL or Book."}), 400

    if data.get('mark_step_complete') and log.step_id:
        step = db.session.get(ProjectStep, log.step_id)
        if step:
            step.is_completed = True
            project = step.project
            if all(s.is_completed for s in project.steps):
                project.is_completed = True

    db.session.commit()
    return jsonify({"status": "success"}), 201

# --- LLM Routes ---

@app.route('/api/llm/plan', methods=['POST'])
def generate_plan():
    print("DEBUG: /api/llm/plan called")
    desc = request.json.get('description')
    if not desc: return jsonify({"error": "No description"}), 400
    return jsonify({"steps": llm_generate_plan(desc)})

@app.route('/api/llm/organize', methods=['POST'])
def organize_notes():
    print("DEBUG: /api/llm/organize called")
    text = request.json.get('text')
    if not text: return jsonify({"error": "No text"}), 400
    return jsonify({"text": llm_organize_notes(text)})

@app.route('/api/llm/generate_questions', methods=['POST'])
def generate_questions():
    print("DEBUG: /api/llm/generate_questions called")
    data = request.json
    text = data.get('text')
    
    if not text: 
        return jsonify({"error": "No text provided"}), 400
        
    questions = llm_generate_questions(text)

    return jsonify({"questions": questions})

@app.route('/api/questions/save', methods=['POST'])
def save_questions():
    data = request.json
    
    # FIX: Force strict type conversion
    try:
        project_id = int(data.get('project_id'))
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid Project ID"}), 400
        
    step_id_raw = data.get('step_id')
    # Convert to int if present and not empty string
    step_id = int(step_id_raw) if step_id_raw and str(step_id_raw).strip() != "" else None
    
    qs = data.get('questions', []) 
    saved_count = 0
    
    print(f"DEBUG: Saving {len(qs)} questions for Project {project_id}, Step {step_id}")

    try:
        for q in qs:
            options = q.get('options', {})
            
            new_q = Question(
                project_id=project_id,
                step_id=step_id,
                question_text=q.get('question_text'),
                option_a=options.get('A') or q.get('option_a'),
                option_b=options.get('B') or q.get('option_b'),
                option_c=options.get('C') or q.get('option_c'),
                option_d=options.get('D') or q.get('option_d'),
                correct_answer=q.get('correct_answer'),
                explanation=q.get('explanation'),
                ease_factor=2.5,
                interval=0,
                repetition_count=0,
                next_review_date=date.today()
            )
            db.session.add(new_q)
            saved_count += 1
            
        db.session.commit()
        print(f"DEBUG: Successfully committed {saved_count} questions.")
        return jsonify({"status": "success", "count": saved_count})
        
    except Exception as e:
        db.session.rollback()
        print(f"ERROR Saving Questions: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/questions/due', methods=['GET'])
def get_due_questions():
    # FIX: Force strict type conversion
    try:
        pid = int(request.args.get('project_id'))
    except (ValueError, TypeError):
        return jsonify([])

    step_ids_raw = request.args.getlist('step_ids[]')
    step_ids = []
    for s in step_ids_raw:
        try:
            step_ids.append(int(s))
        except: pass
    
    print(f"DEBUG: Checking due questions for Project {pid}. Step filter: {step_ids}")
    
    query = db.select(Question).where(
        Question.project_id == pid,
        Question.next_review_date <= date.today()
    )
    if step_ids:
        query = query.where(db.or_(Question.step_id.in_(step_ids), Question.step_id == None))
    
    questions = db.session.execute(query).scalars().all()
    print(f"DEBUG: Found {len(questions)} due questions.")
    
    return jsonify([q.to_dict() for q in questions])

@app.route('/api/questions/<int:qid>/review', methods=['POST'])
def review_question(qid):
    rating = request.json.get('rating')
    q = db.session.get(Question, qid)
    if not q: return jsonify({"error": "Not found"}), 404
    
    quality = {"hard": 3, "good": 4, "easy": 5}.get(rating, 3)
    
    # SM-2 Logic
    new_ef = q.ease_factor + (0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02))
    q.ease_factor = max(1.3, new_ef)
    
    if quality < 3:
        q.repetition_count = 0
        q.interval = 1
    else:
        q.repetition_count += 1
        if q.repetition_count == 1: q.interval = 1
        elif q.repetition_count == 2: q.interval = 6
        else: q.interval = int(q.interval * q.ease_factor)
            
    q.next_review_date = date.today() + timedelta(days=q.interval)
    db.session.commit()
    return jsonify({"status": "success", "next_due": q.next_review_date.isoformat()})


@app.route('/api/dashboard/stats', methods=['GET'])
def get_dashboard_stats():
    # 1. Get Recent Learning Sessions (Logs)
    # We treat every DailyLog as a "Session"
    recent_logs = db.session.execute(
        db.select(DailyLog)
        .order_by(DailyLog.date_val.desc(), DailyLog.id.desc())
        .limit(DASHBOARD_LIMIT)
    ).scalars().all()

    # 2. Get Recently Completed Steps (Proxy)
    # Since we don't have a 'completed_at' date, we look for logs 
    # attached to steps that are now marked as complete.
    completed_steps_query = (
        db.select(DailyLog)
        .join(ProjectStep)
        .where(ProjectStep.is_completed == True)
        .order_by(DailyLog.date_val.desc())
        .limit(DASHBOARD_LIMIT)
    )
    recent_completions = db.session.execute(completed_steps_query).scalars().all()

    return jsonify({
        "recent_sessions": [l.to_dict() for l in recent_logs],
        "recent_completions": [l.to_dict() for l in recent_completions]
    })



# CHECK: Add route to update project details and synchronize steps (Edit/Add/Delete)
@app.route('/api/projects/<int:pid>', methods=['PUT'])
def update_project(pid):
    project = db.session.get(Project, pid)
    if not project:
        return jsonify({"error": "Project not found"}), 404
    
    data = request.json
    
    # Update main fields
    project.name = data.get('name', project.name)
    project.description_raw = data.get('description', project.description_raw)
    
    # Handle Steps Sync
    incoming_steps = data.get('steps', [])
    incoming_ids = [int(s['id']) for s in incoming_steps if 'id' in s]
    
    # 1. Delete steps not present in incoming data
    # (Cascade will handle logs/questions attached to these steps if configured, otherwise manual cleanup might be needed)
    for existing_step in project.steps[:]:
        if existing_step.id not in incoming_ids:
            db.session.delete(existing_step)
            
    # 2. Update existing or Add new
    for idx, step_data in enumerate(incoming_steps):
        if 'id' in step_data:
            # Update existing
            step = db.session.get(ProjectStep, step_data['id'])
            if step:
                step.content = step_data['content']
                step.step_order = idx
        else:
            # Add new (User created a step in UI and saved)
            new_step = ProjectStep(
                project_id=project.id,
                content=step_data['content'],
                step_order=idx
            )
            db.session.add(new_step)

    try:
        db.session.commit()
        return jsonify({"status": "success"})
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500

# CHECK: Add route to delete the entire project
@app.route('/api/projects/<int:pid>', methods=['DELETE'])
def delete_project(pid):
    project = db.session.get(Project, pid)
    if not project:
        return jsonify({"error": "Project not found"}), 404
    
    try:
        db.session.delete(project)
        db.session.commit()
        return jsonify({"status": "success"})
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500





# 3. Delete Log
@app.route('/api/logs/<int:log_id>', methods=['DELETE'])
def delete_log(log_id):
    log = db.session.get(DailyLog, log_id)
    if not log:
        return jsonify({"error": "Log not found"}), 404
    
    try:
        db.session.delete(log)
        db.session.commit()
        return jsonify({"status": "success"})
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500




if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        print(f"✅ Database initialized.")
        print(f"🚀 Server running on http://127.0.0.1:{settings.port}")
    app.run(debug=True, port=settings.port)