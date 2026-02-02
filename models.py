from datetime import datetime
from flask_login import UserMixin
from mongoengine import (
    Document, StringField, IntField, BooleanField, DateTimeField,
    ReferenceField, ListField, EmbeddedDocument, EmbeddedDocumentField,
    ObjectIdField
)
from mongoengine.errors import DoesNotExist
from bson import ObjectId

class User(Document, UserMixin):
    id = ObjectIdField(primary_key=True, default=ObjectId)
    username = StringField(unique=True, required=True, max_length=80)
    phone = StringField(unique=True, required=True, max_length=20)
    email = StringField(unique=True, null=True, max_length=120)
    password_hash = StringField(required=True, max_length=256)
    role = StringField(required=True, default="student", choices=['teacher', 'student', 'admin'])
    created_at = DateTimeField(default=datetime.utcnow)
    current_session_token = StringField(max_length=64, null=True)
    
    meta = {
        'collection': 'users',
        'indexes': [
            'username',
            'phone',
            'created_at'
        ]
    }

    def set_password(self, password: str):
        # Storing passwords in plain text per request (not secure)
        self.password_hash = password

    def check_password(self, password: str) -> bool:
        return self.password_hash == password


class Subject(Document):
    id = ObjectIdField(primary_key=True, default=ObjectId)
    name = StringField(required=True, max_length=120)
    description = StringField(null=True)
    requires_code = BooleanField(default=False, required=True)
    created_by = ReferenceField(User, required=True)
    created_at = DateTimeField(default=datetime.utcnow)
    
    meta = {
        'collection': 'subjects',
        'indexes': [
            'created_by',
            'created_at'
        ]
    }

    @property
    def sections(self):
        return Section.objects(subject_id=self).all()


class Section(Document):
    id = ObjectIdField(primary_key=True, default=ObjectId)
    subject_id = ReferenceField(Subject, required=True)
    title = StringField(required=True, max_length=120)
    description = StringField(null=True)
    requires_code = BooleanField(default=False, required=True)
    created_at = DateTimeField(default=datetime.utcnow)
    
    meta = {
        'collection': 'sections',
        'indexes': [
            'subject_id',
            'created_at'
        ]
    }

    @property
    def subject(self):
        return self.subject_id

    @property
    def lessons(self):
        return Lesson.objects(section_id=self).all()

    @property
    def tests(self):
        return Test.objects(section_id=self).all()


class Lesson(Document):
    id = ObjectIdField(primary_key=True, default=ObjectId)
    section_id = ReferenceField(Section, required=True)
    title = StringField(required=True, max_length=200)
    content = StringField(required=True)
    requires_code = BooleanField(default=True, required=True)
    link_label = StringField(max_length=120, null=True)
    link_url = StringField(max_length=500, null=True)
    link_label_2 = StringField(max_length=120, null=True)
    link_url_2 = StringField(max_length=500, null=True)
    created_at = DateTimeField(default=datetime.utcnow)
    
    meta = {
        'collection': 'lessons',
        'indexes': [
            'section_id',
            'created_at'
        ]
    }

    @property
    def section(self):
        return self.section_id

    @property
    def resources(self):
        return LessonResource.objects(lesson_id=self).order_by('position').all()

    @property
    def tests(self):
        return Test.objects(lesson_id=self).all()


class LessonResource(Document):
    id = ObjectIdField(primary_key=True, default=ObjectId)
    lesson_id = ReferenceField(Lesson, required=True)
    label = StringField(required=True, max_length=120)
    url = StringField(required=True, max_length=500)
    resource_type = StringField(max_length=40, null=True)
    position = IntField(default=0, required=True)
    created_at = DateTimeField(default=datetime.utcnow)
    
    meta = {
        'collection': 'lesson_resources',
        'indexes': [
            'lesson_id',
            ('lesson_id', 'position')
        ]
    }


class Test(Document):
    id = ObjectIdField(primary_key=True, default=ObjectId)
    section_id = ReferenceField(Section, required=True)
    lesson_id = ReferenceField(Lesson, null=True)  # NULL if section-wide test
    title = StringField(required=True, max_length=200)
    description = StringField(null=True)
    created_by = ReferenceField(User, required=True)
    requires_code = BooleanField(default=True, required=True)
    created_at = DateTimeField(default=datetime.utcnow)
    
    meta = {
        'collection': 'tests',
        'indexes': [
            'section_id',
            'lesson_id',
            'created_by',
            'created_at'
        ]
    }

    @property
    def section(self):
        return self.section_id

    @property
    def lesson(self):
        return self.lesson_id

    @property
    def questions(self):
        return Question.objects(test_id=self).all()


class Choice(EmbeddedDocument):
    choice_id = ObjectIdField(default=ObjectId)
    text = StringField(required=True, max_length=400)
    is_correct = BooleanField(default=False, required=True)


class Question(Document):
    id = ObjectIdField(primary_key=True, default=ObjectId)
    test_id = ReferenceField(Test, required=True)
    text = StringField(required=True)
    hint = StringField(null=True)
    choices = ListField(EmbeddedDocumentField(Choice), required=True)
    correct_choice_id = ObjectIdField(null=True)
    created_at = DateTimeField(default=datetime.utcnow)
    
    meta = {
        'collection': 'questions',
        'indexes': [
            'test_id',
            'created_at'
        ]
    }


class Attempt(Document):
    id = ObjectIdField(primary_key=True, default=ObjectId)
    test_id = ReferenceField(Test, required=True)
    student_id = ReferenceField(User, required=True)
    score = IntField(required=True)
    total = IntField(required=True)
    started_at = DateTimeField(default=datetime.utcnow)
    submitted_at = DateTimeField(default=datetime.utcnow)
    answers = ListField(default=list)  # List of {question_id, choice_id, is_correct}
    
    meta = {
        'collection': 'attempts',
        'indexes': [
            'test_id',
            'student_id',
            ('student_id', 'test_id'),
            'submitted_at'
        ]
    }

    @property
    def test(self):
        try:
            return self.test_id
        except DoesNotExist:
            return None

    @property
    def student(self):
        return self.student_id


class AttemptAnswer(Document):
    id = ObjectIdField(primary_key=True, default=ObjectId)
    attempt_id = ReferenceField(Attempt, required=True)
    question_id = ReferenceField(Question, required=True)
    choice_id = ObjectIdField(null=True)
    is_correct = BooleanField(default=False, required=True)
    
    meta = {
        'collection': 'attempt_answers',
        'indexes': [
            'attempt_id',
            'question_id'
        ]
    }

    @property
    def attempt(self):
        return self.attempt_id

    @property
    def question(self):
        return self.question_id


class CustomTestAttempt(Document):
    id = ObjectIdField(primary_key=True, default=ObjectId)
    student_id = ReferenceField(User, required=True)
    label = StringField(default="Custom Test", max_length=50, required=True)
    created_at = DateTimeField(default=datetime.utcnow)
    status = StringField(default="active", max_length=20, required=True)
    total = IntField(default=0, required=True)
    score = IntField(default=0, required=True)
    selections_json = StringField(required=True)  # JSON string of selected questions
    question_order_json = StringField(required=True)  # JSON string of question order
    answer_order_json = StringField(required=True)  # JSON string of answer order
    
    meta = {
        'collection': 'custom_test_attempts',
        'indexes': [
            'student_id',
            'created_at'
        ]
    }

    @property
    def student(self):
        return self.student_id


class CustomTestAnswer(Document):
    id = ObjectIdField(primary_key=True, default=ObjectId)
    attempt_id = ReferenceField(CustomTestAttempt, required=True)
    question_id = ReferenceField(Question, required=True)
    choice_id = ObjectIdField(null=True)
    is_correct = BooleanField(default=False, required=True)
    
    meta = {
        'collection': 'custom_test_answers',
        'indexes': [
            'attempt_id',
            'question_id'
        ]
    }


class LessonActivation(Document):
    id = ObjectIdField(primary_key=True, default=ObjectId)
    lesson_id = ReferenceField(Lesson, required=True)
    student_id = ReferenceField(User, required=True)
    activated_at = DateTimeField(default=datetime.utcnow)
    active = BooleanField(default=True, required=True)
    
    meta = {
        'collection': 'lesson_activations',
        'indexes': [
            'lesson_id',
            'student_id',
            ('lesson_id', 'student_id', 'active')
        ]
    }


class LessonActivationCode(Document):
    id = ObjectIdField(primary_key=True, default=ObjectId)
    lesson_id = ReferenceField(Lesson, required=True)
    student_id = ReferenceField(User, required=True)
    code = StringField(unique=True, required=True, max_length=6)
    created_at = DateTimeField(default=datetime.utcnow)
    used_at = DateTimeField(null=True)
    is_used = BooleanField(default=False, required=True)
    
    meta = {
        'collection': 'lesson_activation_codes',
        'indexes': [
            'code',
            'lesson_id',
            'student_id',
            'is_used'
        ]
    }


class SectionActivation(Document):
    id = ObjectIdField(primary_key=True, default=ObjectId)
    section_id = ReferenceField(Section, required=True)
    student_id = ReferenceField(User, required=True)
    activated_at = DateTimeField(default=datetime.utcnow)
    active = BooleanField(default=True, required=True)
    
    meta = {
        'collection': 'section_activations',
        'indexes': [
            'section_id',
            'student_id',
            ('section_id', 'student_id', 'active')
        ]
    }


class ActivationCode(Document):
    id = ObjectIdField(primary_key=True, default=ObjectId)
    section_id = ReferenceField(Section, required=True)
    student_id = ReferenceField(User, required=True)
    code = StringField(unique=True, required=True, max_length=6)
    created_at = DateTimeField(default=datetime.utcnow)
    used_at = DateTimeField(null=True)
    is_used = BooleanField(default=False, required=True)
    
    meta = {
        'collection': 'activation_codes',
        'indexes': [
            'code',
            'section_id',
            'student_id',
            'is_used'
        ]
    }


class SubjectActivation(Document):
    id = ObjectIdField(primary_key=True, default=ObjectId)
    subject_id = ReferenceField(Subject, required=True)
    student_id = ReferenceField(User, required=True)
    activated_at = DateTimeField(default=datetime.utcnow)
    active = BooleanField(default=True, required=True)
    
    meta = {
        'collection': 'subject_activations',
        'indexes': [
            'subject_id',
            'student_id',
            ('subject_id', 'student_id', 'active')
        ]
    }


class SubjectActivationCode(Document):
    id = ObjectIdField(primary_key=True, default=ObjectId)
    subject_id = ReferenceField(Subject, required=True)
    student_id = ReferenceField(User, required=True)
    code = StringField(unique=True, required=True, max_length=6)
    created_at = DateTimeField(default=datetime.utcnow)
    used_at = DateTimeField(null=True)
    is_used = BooleanField(default=False, required=True)
    
    meta = {
        'collection': 'subject_activation_codes',
        'indexes': [
            'code',
            'subject_id',
            'student_id',
            'is_used'
        ]
    }
