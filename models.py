from datetime import datetime
from flask_login import UserMixin

from .extensions import db

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="student")  # 'teacher' or 'student'
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    current_session_token = db.Column(db.String(64), nullable=True)

    subjects = db.relationship("Subject", backref="creator", lazy=True)
    tests = db.relationship("Test", backref="creator", lazy=True)
    attempts = db.relationship("Attempt", backref="student", lazy=True)

    def set_password(self, password: str):
        # Storing passwords in plain text per request (not secure)
        self.password_hash = password

    def check_password(self, password: str) -> bool:
        return self.password_hash == password

class Subject(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    description = db.Column(db.Text, nullable=True)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)

    sections = db.relationship("Section", backref="subject", cascade="all, delete-orphan", lazy=True)

class Section(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    subject_id = db.Column(db.Integer, db.ForeignKey("subject.id"), nullable=False)
    title = db.Column(db.String(120), nullable=False)
    description = db.Column(db.Text, nullable=True)
    requires_code = db.Column(db.Boolean, default=False, nullable=False)

    lessons = db.relationship("Lesson", backref="section", cascade="all, delete-orphan", lazy=True)
    tests = db.relationship("Test", backref="section", cascade="all, delete-orphan", lazy=True)

class Lesson(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    section_id = db.Column(db.Integer, db.ForeignKey("section.id"), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    content = db.Column(db.Text, nullable=False)
    requires_code = db.Column(db.Boolean, default=True, nullable=False)
    link_label = db.Column(db.String(120), nullable=True)
    link_url = db.Column(db.String(500), nullable=True)
    link_label_2 = db.Column(db.String(120), nullable=True)
    link_url_2 = db.Column(db.String(500), nullable=True)

    resources = db.relationship(
        "LessonResource",
        backref="lesson",
        cascade="all, delete-orphan",
        lazy=True,
        order_by="LessonResource.position",
    )

    tests = db.relationship("Test", backref="lesson", lazy=True)


class LessonResource(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    lesson_id = db.Column(db.Integer, db.ForeignKey("lesson.id"), nullable=False)
    label = db.Column(db.String(120), nullable=False)
    url = db.Column(db.String(500), nullable=False)
    resource_type = db.Column(db.String(40), nullable=True)
    position = db.Column(db.Integer, nullable=False, default=0)

class Test(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    section_id = db.Column(db.Integer, db.ForeignKey("section.id"), nullable=False)
    lesson_id = db.Column(db.Integer, db.ForeignKey("lesson.id"), nullable=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=True)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)

    questions = db.relationship("Question", backref="test", cascade="all, delete-orphan", lazy=True)
    attempts = db.relationship("Attempt", backref="test", cascade="all, delete-orphan", lazy=True)

    requires_code = db.Column(db.Boolean, default=True, nullable=False)

class Question(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    test_id = db.Column(db.Integer, db.ForeignKey("test.id"), nullable=False)
    text = db.Column(db.Text, nullable=False)
    hint = db.Column(db.Text, nullable=True)

    choices = db.relationship("Choice", backref="question", cascade="all, delete-orphan", lazy=True)

class Choice(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    question_id = db.Column(db.Integer, db.ForeignKey("question.id"), nullable=False)
    text = db.Column(db.String(400), nullable=False)
    is_correct = db.Column(db.Boolean, default=False, nullable=False)

class Attempt(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    test_id = db.Column(db.Integer, db.ForeignKey("test.id"), nullable=False)
    student_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    score = db.Column(db.Integer, nullable=False)
    total = db.Column(db.Integer, nullable=False)
    started_at = db.Column(db.DateTime, default=datetime.utcnow)
    submitted_at = db.Column(db.DateTime, default=datetime.utcnow)

    answers = db.relationship("AttemptAnswer", backref="attempt", cascade="all, delete-orphan", lazy=True)

class AttemptAnswer(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    attempt_id = db.Column(db.Integer, db.ForeignKey("attempt.id"), nullable=False)
    question_id = db.Column(db.Integer, db.ForeignKey("question.id"), nullable=False)
    choice_id = db.Column(db.Integer, db.ForeignKey("choice.id"), nullable=True)
    is_correct = db.Column(db.Boolean, default=False, nullable=False)


class CustomTestAttempt(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    label = db.Column(db.String(50), nullable=False, default="Custom Test")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    status = db.Column(db.String(20), nullable=False, default="active")
    total = db.Column(db.Integer, nullable=False, default=0)
    score = db.Column(db.Integer, nullable=False, default=0)
    selections_json = db.Column(db.Text, nullable=False)
    question_order_json = db.Column(db.Text, nullable=False)
    answer_order_json = db.Column(db.Text, nullable=False)

    answers = db.relationship("CustomTestAnswer", backref="attempt", cascade="all, delete-orphan", lazy=True)


class CustomTestAnswer(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    attempt_id = db.Column(db.Integer, db.ForeignKey("custom_test_attempt.id"), nullable=False)
    question_id = db.Column(db.Integer, db.ForeignKey("question.id"), nullable=False)
    choice_id = db.Column(db.Integer, db.ForeignKey("choice.id"), nullable=True)
    is_correct = db.Column(db.Boolean, default=False, nullable=False)


class TestActivation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    test_id = db.Column(db.Integer, db.ForeignKey("test.id"), nullable=False)
    student_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    activated_at = db.Column(db.DateTime, default=datetime.utcnow)
    active = db.Column(db.Boolean, default=True, nullable=False)


class TestActivationCode(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    test_id = db.Column(db.Integer, db.ForeignKey("test.id"), nullable=False)
    student_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    code = db.Column(db.String(6), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    used_at = db.Column(db.DateTime, nullable=True)
    is_used = db.Column(db.Boolean, default=False, nullable=False)


class LessonActivation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    lesson_id = db.Column(db.Integer, db.ForeignKey("lesson.id"), nullable=False)
    student_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    activated_at = db.Column(db.DateTime, default=datetime.utcnow)
    active = db.Column(db.Boolean, default=True, nullable=False)


class LessonActivationCode(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    lesson_id = db.Column(db.Integer, db.ForeignKey("lesson.id"), nullable=False)
    student_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    code = db.Column(db.String(6), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    used_at = db.Column(db.DateTime, nullable=True)
    is_used = db.Column(db.Boolean, default=False, nullable=False)


class SectionActivation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    section_id = db.Column(db.Integer, db.ForeignKey("section.id"), nullable=False)
    student_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    activated_at = db.Column(db.DateTime, default=datetime.utcnow)
    active = db.Column(db.Boolean, default=True, nullable=False)


class ActivationCode(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    section_id = db.Column(db.Integer, db.ForeignKey("section.id"), nullable=False)
    student_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    code = db.Column(db.String(6), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    used_at = db.Column(db.DateTime, nullable=True)
    is_used = db.Column(db.Boolean, default=False, nullable=False)
