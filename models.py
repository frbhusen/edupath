from datetime import datetime
from flask_login import UserMixin
from mongoengine import (
    Document, StringField, IntField, BooleanField, DateTimeField,
    ReferenceField, ListField, EmbeddedDocument, EmbeddedDocumentField,
    ObjectIdField, DictField
)
from mongoengine.errors import DoesNotExist
from bson import ObjectId

class User(Document, UserMixin):
    id = ObjectIdField(primary_key=True, default=ObjectId)
    first_name = StringField(required=True, max_length=80)
    last_name = StringField(required=True, max_length=80)
    username = StringField(unique=True, required=True, max_length=80)
    phone = StringField(unique=True, required=True, max_length=10)
    email = StringField(unique=False, sparse=True, null=True, max_length=120)
    password_hash = StringField(required=True, max_length=256)
    role = StringField(required=True, default="student", choices=['admin', 'teacher', 'question_editor', 'student'])
    created_at = DateTimeField(default=datetime.utcnow)
    current_session_token = StringField(max_length=64, null=True)
    
    meta = {
        'collection': 'users',
        'strict': False,
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

    @property
    def full_name(self) -> str:
        first = (self.first_name or "").strip()
        last = (self.last_name or "").strip()
        full = f"{first} {last}".strip()
        return full or self.username

    @property
    def display_name(self) -> str:
        return self.full_name


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


class StaffSubjectAccess(Document):
    id = ObjectIdField(primary_key=True, default=ObjectId)
    staff_user_id = ReferenceField(User, required=True)
    subject_id = ReferenceField(Subject, required=True)
    assigned_by = ReferenceField(User, null=True)
    active = BooleanField(default=True, required=True)
    assigned_at = DateTimeField(default=datetime.utcnow)
    updated_at = DateTimeField(default=datetime.utcnow)

    meta = {
        'collection': 'staff_subject_access',
        'indexes': [
            'staff_user_id',
            'subject_id',
            'active',
            ('staff_user_id', 'subject_id')
        ]
    }


class StaffSubjectAccessAudit(Document):
    id = ObjectIdField(primary_key=True, default=ObjectId)
    staff_user_id = ReferenceField(User, required=True)
    changed_by = ReferenceField(User, required=True)
    before_subject_ids = ListField(ObjectIdField(), default=list)
    after_subject_ids = ListField(ObjectIdField(), default=list)
    note = StringField(max_length=240, null=True)
    created_at = DateTimeField(default=datetime.utcnow)

    meta = {
        'collection': 'staff_subject_access_audit',
        'indexes': [
            'staff_user_id',
            'changed_by',
            'created_at'
        ]
    }


class StaffActivityLog(Document):
    id = ObjectIdField(primary_key=True, default=ObjectId)
    staff_user_id = ReferenceField(User, required=True)
    staff_role = StringField(required=True, max_length=40)
    endpoint = StringField(required=True, max_length=120)
    action = StringField(required=True, max_length=120)
    http_method = StringField(required=True, max_length=10)
    path = StringField(required=True, max_length=300)
    target_type = StringField(max_length=80, null=True)
    target_id = StringField(max_length=80, null=True)
    details = StringField(max_length=500, null=True)
    status_code = IntField(required=True, default=200)
    success = BooleanField(required=True, default=True)
    created_at = DateTimeField(default=datetime.utcnow)

    meta = {
        'collection': 'staff_activity_logs',
        'indexes': [
            'staff_user_id',
            'staff_role',
            'endpoint',
            'http_method',
            'target_type',
            'success',
            'created_at',
        ]
    }


class Notification(Document):
    id = ObjectIdField(primary_key=True, default=ObjectId)
    title = StringField(required=True, max_length=180)
    body = StringField(required=True, max_length=3000)
    template_type = StringField(required=True, default='note', choices=['note', 'info', 'success', 'warning', 'urgent'])
    audience = StringField(required=True, choices=['all', 'students', 'staff', 'specific'])
    created_by = ReferenceField(User, required=True)
    created_at = DateTimeField(default=datetime.utcnow)

    meta = {
        'collection': 'notifications',
        'indexes': [
            'audience',
            'created_by',
            'created_at',
        ]
    }


class NotificationRecipient(Document):
    id = ObjectIdField(primary_key=True, default=ObjectId)
    notification_id = ReferenceField(Notification, required=True)
    user_id = ReferenceField(User, required=True)
    is_read = BooleanField(default=False, required=True)
    read_at = DateTimeField(null=True)
    created_at = DateTimeField(default=datetime.utcnow)

    meta = {
        'collection': 'notification_recipients',
        'indexes': [
            'notification_id',
            'user_id',
            'is_read',
            'created_at',
            ('notification_id', 'user_id'),
        ]
    }


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
    allow_full_lesson_test = BooleanField(default=False, required=True)
    xp_reward = IntField(default=10, required=True)
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

    @property
    def resources(self):
        return TestResource.objects(test_id=self).order_by('position').all()


class TestResource(Document):
    id = ObjectIdField(primary_key=True, default=ObjectId)
    test_id = ReferenceField(Test, required=True)
    label = StringField(required=True, max_length=120)
    url = StringField(required=True, max_length=500)
    resource_type = StringField(max_length=40, null=True)
    position = IntField(default=0, required=True)
    created_at = DateTimeField(default=datetime.utcnow)

    meta = {
        'collection': 'test_resources',
        'indexes': [
            'test_id',
            ('test_id', 'position')
        ]
    }


class Choice(EmbeddedDocument):
    choice_id = ObjectIdField(default=ObjectId)
    text = StringField(required=True, max_length=400)
    image_url = StringField(max_length=500, null=True)
    is_correct = BooleanField(default=False, required=True)


class Question(Document):
    id = ObjectIdField(primary_key=True, default=ObjectId)
    test_id = ReferenceField(Test, required=True)
    text = StringField(required=True)
    question_images = ListField(StringField(max_length=500))
    hint = StringField(null=True)
    difficulty = StringField(default="medium", choices=["easy", "medium", "hard"])
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


class TestTextQuestion(Document):
    id = ObjectIdField(primary_key=True, default=ObjectId)
    test_id = ReferenceField(Test, required=True)
    text = StringField(required=True)
    hint = StringField(null=True)
    max_score = IntField(default=5, required=True)
    created_at = DateTimeField(default=datetime.utcnow)

    meta = {
        'collection': 'test_text_questions',
        'indexes': [
            'test_id',
            'created_at'
        ]
    }


class TestInteractiveQuestion(Document):
    id = ObjectIdField(primary_key=True, default=ObjectId)
    test_id = ReferenceField(Test, required=True)
    question_text = StringField(null=True)
    question_image_url = StringField(max_length=500, null=True)
    answer_text = StringField(null=True)
    answer_image_url = StringField(max_length=500, null=True)
    difficulty = StringField(default="medium", choices=["easy", "medium", "hard"])
    correct_value = BooleanField(required=True, default=True)
    created_at = DateTimeField(default=datetime.utcnow)

    meta = {
        'collection': 'test_interactive_questions',
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
    question_order_json = StringField(null=True)
    selection_settings_json = StringField(null=True)
    is_retake = BooleanField(default=False)
    xp_earned = IntField(default=0)
    
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


class AttemptTextAnswer(Document):
    id = ObjectIdField(primary_key=True, default=ObjectId)
    attempt_id = ReferenceField(Attempt, required=True)
    text_question_id = ReferenceField(TestTextQuestion, required=True)
    answer_text = StringField(required=True)
    max_score = IntField(default=5, required=True)
    score_awarded = IntField(null=True)
    created_at = DateTimeField(default=datetime.utcnow)

    meta = {
        'collection': 'attempt_text_answers',
        'indexes': [
            'attempt_id',
            'text_question_id',
            ('attempt_id', 'text_question_id')
        ]
    }


class AttemptInteractiveAnswer(Document):
    id = ObjectIdField(primary_key=True, default=ObjectId)
    attempt_id = ReferenceField(Attempt, required=True)
    interactive_question_id = ReferenceField(TestInteractiveQuestion, required=True)
    selected_value = BooleanField(required=True, default=False)
    is_correct = BooleanField(required=True, default=False)
    created_at = DateTimeField(default=datetime.utcnow)

    meta = {
        'collection': 'attempt_interactive_answers',
        'indexes': [
            'attempt_id',
            'interactive_question_id',
            ('attempt_id', 'interactive_question_id')
        ]
    }


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
    is_retake = BooleanField(default=False)
    xp_earned = IntField(default=0)
    
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
    question_id = ReferenceField(Question, null=True)
    interactive_question_id = ReferenceField(TestInteractiveQuestion, null=True)
    choice_id = ObjectIdField(null=True)
    selected_value = BooleanField(null=True)
    is_correct = BooleanField(default=False, required=True)
    
    meta = {
        'collection': 'custom_test_answers',
        'indexes': [
            'attempt_id',
            'question_id',
            'interactive_question_id'
        ]
    }


class CourseSet(Document):
    id = ObjectIdField(primary_key=True, default=ObjectId)
    subject_id = ReferenceField(Subject, required=True)
    section_id = ReferenceField(Section, null=True)
    lesson_id = ReferenceField(Lesson, null=True)
    title = StringField(required=True, max_length=200)
    description = StringField(null=True)
    link_label = StringField(max_length=120, null=True)
    link_url = StringField(max_length=500, null=True)
    link_label_2 = StringField(max_length=120, null=True)
    link_url_2 = StringField(max_length=500, null=True)
    created_by = ReferenceField(User, required=True)
    xp_per_question = IntField(default=1, required=True)
    is_active = BooleanField(default=True, required=True)
    created_at = DateTimeField(default=datetime.utcnow)

    meta = {
        'collection': 'course_sets',
        'indexes': [
            'subject_id',
            'section_id',
            'lesson_id',
            'created_by',
            'is_active',
            'created_at',
        ]
    }


class CourseQuestion(Document):
    id = ObjectIdField(primary_key=True, default=ObjectId)
    course_set_id = ReferenceField(CourseSet, required=True)
    question_type = StringField(default='interactive', choices=['interactive', 'mcq'], required=True)
    question_text = StringField(null=True)
    question_image_url = StringField(max_length=500, null=True)
    answer_text = StringField(null=True)
    answer_image_url = StringField(max_length=500, null=True)
    choices = ListField(EmbeddedDocumentField(Choice), default=list)
    correct_choice_id = ObjectIdField(null=True)
    correct_value = BooleanField(required=True, default=True)
    created_at = DateTimeField(default=datetime.utcnow)

    meta = {
        'collection': 'course_questions',
        'indexes': [
            'course_set_id',
            'created_at',
        ]
    }


class CourseAttempt(Document):
    id = ObjectIdField(primary_key=True, default=ObjectId)
    course_set_id = ReferenceField(CourseSet, required=True)
    student_id = ReferenceField(User, required=True)
    status = StringField(default='submitted', max_length=20, required=True)
    total = IntField(default=0, required=True)
    score = IntField(default=0, required=True)
    xp_earned = IntField(default=0, required=True)
    created_at = DateTimeField(default=datetime.utcnow)

    meta = {
        'collection': 'course_attempts',
        'indexes': [
            'course_set_id',
            'student_id',
            ('student_id', 'course_set_id'),
            'created_at',
        ]
    }


class CourseAnswer(Document):
    id = ObjectIdField(primary_key=True, default=ObjectId)
    attempt_id = ReferenceField(CourseAttempt, required=True)
    question_id = ReferenceField(CourseQuestion, required=True)
    choice_id = ObjectIdField(null=True)
    selected_value = BooleanField(null=True)
    is_correct = BooleanField(required=True, default=False)
    created_at = DateTimeField(default=datetime.utcnow)

    meta = {
        'collection': 'course_answers',
        'indexes': [
            'attempt_id',
            'question_id',
        ]
    }


class StudentGamification(Document):
    id = ObjectIdField(primary_key=True, default=ObjectId)
    student_id = ReferenceField(User, required=True, unique=True)
    xp_total = IntField(default=0, required=True)
    level = IntField(default=1, required=True)
    current_streak = IntField(default=0, required=True)
    best_streak = IntField(default=0, required=True)
    last_activity_date = DateTimeField(null=True)
    badges = ListField(StringField(max_length=64), default=list)
    updated_at = DateTimeField(default=datetime.utcnow)

    meta = {
        'collection': 'student_gamification',
        'indexes': [
            'student_id',
            'xp_total',
            'level',
            {'fields': ['-xp_total', 'student_id']}
        ]
    }


class XPEvent(Document):
    id = ObjectIdField(primary_key=True, default=ObjectId)
    student_id = ReferenceField(User, required=True)
    event_type = StringField(required=True, max_length=64)
    source_id = StringField(required=True, max_length=64)
    xp = IntField(required=True)
    created_at = DateTimeField(default=datetime.utcnow)

    meta = {
        'collection': 'xp_events',
        'indexes': [
            'student_id',
            'event_type',
            'source_id',
            'created_at',
            ('student_id', 'event_type', 'source_id')
        ]
    }


class Duel(Document):
    id = ObjectIdField(primary_key=True, default=ObjectId)
    challenger_id = ReferenceField(User, required=True)
    opponent_id = ReferenceField(User, required=True)
    opponent_username_snapshot = StringField(required=True, max_length=80)

    scope_type = StringField(required=True, choices=["lesson", "section", "subject"])
    scope_id = ObjectIdField(required=True)
    scope_title = StringField(required=True, max_length=220)

    invite_token = StringField(required=True, unique=True, max_length=64)
    invite_consumed = BooleanField(default=False, required=True)
    status = StringField(
        required=True,
        default="pending",
        choices=["pending", "accepted_waiting", "live", "completed", "declined", "expired", "canceled"],
    )

    question_ids_json = StringField(required=True)
    question_count = IntField(default=15, required=True)
    timer_seconds = IntField(default=540, required=True)
    entry_fee_xp = IntField(default=20, required=True)

    challenger_joined_at = DateTimeField(null=True)
    opponent_joined_at = DateTimeField(null=True)
    started_at = DateTimeField(null=True)
    ended_at = DateTimeField(null=True)
    expires_at = DateTimeField(required=True)
    created_at = DateTimeField(default=datetime.utcnow)

    challenger_submitted = BooleanField(default=False, required=True)
    opponent_submitted = BooleanField(default=False, required=True)
    challenger_finished_at = DateTimeField(null=True)
    opponent_finished_at = DateTimeField(null=True)

    challenger_score = IntField(default=0, required=True)
    opponent_score = IntField(default=0, required=True)
    winner_id = ReferenceField(User, null=True)

    first_submitter_slot = StringField(choices=["challenger", "opponent"], null=True)
    first_submitter_perfect = BooleanField(default=False, required=True)
    second_submitter_perfect = BooleanField(default=False, required=True)

    challenger_penalty_seconds = IntField(default=0, required=True)
    opponent_penalty_seconds = IntField(default=0, required=True)

    fee_applied = BooleanField(default=False, required=True)
    settled = BooleanField(default=False, required=True)
    settlement_json = StringField(null=True)

    meta = {
        'collection': 'duels',
        'indexes': [
            'challenger_id',
            'opponent_id',
            'invite_token',
            'status',
            'expires_at',
            'created_at',
            ('challenger_id', 'opponent_id', 'status')
        ]
    }


class DuelAnswer(Document):
    id = ObjectIdField(primary_key=True, default=ObjectId)
    duel_id = ReferenceField(Duel, required=True)
    player_id = ReferenceField(User, required=True)
    question_id = ReferenceField(Question, required=True)
    choice_id = ObjectIdField(null=True)
    is_correct = BooleanField(default=False, required=True)
    created_at = DateTimeField(default=datetime.utcnow)

    meta = {
        'collection': 'duel_answers',
        'indexes': [
            'duel_id',
            'player_id',
            'question_id',
            ('duel_id', 'player_id', 'question_id')
        ]
    }


class DuelStats(Document):
    id = ObjectIdField(primary_key=True, default=ObjectId)
    student_id = ReferenceField(User, required=True, unique=True)
    wins = IntField(default=0, required=True)
    losses = IntField(default=0, required=True)
    current_win_streak = IntField(default=0, required=True)
    best_win_streak = IntField(default=0, required=True)
    total_duels = IntField(default=0, required=True)
    updated_at = DateTimeField(default=datetime.utcnow)

    meta = {
        'collection': 'duel_stats',
        'indexes': [
            'student_id',
            'wins',
            'current_win_streak',
            {'fields': ['-wins', '-current_win_streak', 'student_id']}
        ]
    }


class LessonCompletion(Document):
    id = ObjectIdField(primary_key=True, default=ObjectId)
    lesson_id = ReferenceField(Lesson, required=True)
    student_id = ReferenceField(User, required=True)
    completed_at = DateTimeField(default=datetime.utcnow)

    meta = {
        'collection': 'lesson_completions',
        'indexes': [
            'lesson_id',
            'student_id',
            ('lesson_id', 'student_id')
        ]
    }


class DiscussionQuestion(Document):
    id = ObjectIdField(primary_key=True, default=ObjectId)
    lesson_id = ReferenceField(Lesson, required=True)
    author_id = ReferenceField(User, required=True)
    title = StringField(required=True, max_length=220)
    body = StringField(required=True)
    is_pinned = BooleanField(default=False, required=True)
    is_resolved = BooleanField(default=False, required=True)
    created_at = DateTimeField(default=datetime.utcnow)

    meta = {
        'collection': 'discussion_questions',
        'indexes': [
            'lesson_id',
            'author_id',
            'is_pinned',
            'is_resolved',
            'created_at'
        ]
    }


class DiscussionAnswer(Document):
    id = ObjectIdField(primary_key=True, default=ObjectId)
    question_id = ReferenceField(DiscussionQuestion, required=True)
    author_id = ReferenceField(User, required=True)
    body = StringField(required=True)
    created_at = DateTimeField(default=datetime.utcnow)

    meta = {
        'collection': 'discussion_answers',
        'indexes': [
            'question_id',
            'author_id',
            'created_at'
        ]
    }


class Certificate(Document):
    id = ObjectIdField(primary_key=True, default=ObjectId)
    student_id = ReferenceField(User, required=True)
    lesson_id = ReferenceField(Lesson, required=True)
    certificate_url = StringField(max_length=1000, null=True)
    is_verified = BooleanField(default=False, required=True)
    verified_by = ReferenceField(User, null=True)
    verified_at = DateTimeField(null=True)
    issued_at = DateTimeField(default=datetime.utcnow)

    meta = {
        'collection': 'certificates',
        'indexes': [
            'student_id',
            'lesson_id',
            'is_verified',
            ('student_id', 'lesson_id'),
            'issued_at'
        ]
    }


class Assignment(Document):
    id = ObjectIdField(primary_key=True, default=ObjectId)
    title = StringField(required=True, max_length=200)
    description = StringField(null=True)
    subject_id = ReferenceField(Subject, null=True)
    section_id = ReferenceField(Section, null=True)
    lesson_id = ReferenceField(Lesson, null=True)
    target_student_id = ReferenceField(User, null=True)
    assignment_mode = StringField(default="standard", required=True, choices=["standard", "custom_test"])
    selected_question_ids_json = StringField(null=True)
    written_questions_json = StringField(null=True)
    max_score = IntField(default=0, required=True)
    due_at = DateTimeField(null=True)
    is_active = BooleanField(default=True, required=True)
    created_by = ReferenceField(User, required=True)
    created_at = DateTimeField(default=datetime.utcnow)

    meta = {
        'collection': 'assignments',
        'indexes': [
            'target_student_id',
            'subject_id',
            'section_id',
            'lesson_id',
            'due_at',
            'assignment_mode',
            'is_active',
            'created_at'
        ]
    }


class AssignmentSubmission(Document):
    id = ObjectIdField(primary_key=True, default=ObjectId)
    assignment_id = ReferenceField(Assignment, required=True)
    student_id = ReferenceField(User, required=True)
    status = StringField(default="pending", required=True, choices=["pending", "completed"])
    note = StringField(null=True)
    completed_at = DateTimeField(null=True)
    created_at = DateTimeField(default=datetime.utcnow)

    meta = {
        'collection': 'assignment_submissions',
        'indexes': [
            'assignment_id',
            'student_id',
            ('assignment_id', 'student_id'),
            'status',
            'completed_at'
        ]
    }


class AssignmentAttempt(Document):
    id = ObjectIdField(primary_key=True, default=ObjectId)
    assignment_id = ReferenceField(Assignment, required=True)
    student_id = ReferenceField(User, required=True)
    answers_json = StringField(required=True)
    status = StringField(default="submitted", required=True, choices=["submitted", "graded"])
    total_score = IntField(default=0, required=True)
    score_awarded = IntField(default=0, required=True)
    teacher_note = StringField(null=True)
    graded_by = ReferenceField(User, null=True)
    submitted_at = DateTimeField(default=datetime.utcnow)
    graded_at = DateTimeField(null=True)

    meta = {
        'collection': 'assignment_attempts',
        'indexes': [
            'assignment_id',
            'student_id',
            ('assignment_id', 'student_id'),
            'status',
            'submitted_at',
            'graded_at'
        ]
    }


class StudyPlan(Document):
    id = ObjectIdField(primary_key=True, default=ObjectId)
    student_id = ReferenceField(User, required=True)
    title = StringField(required=True, max_length=200)
    description = StringField(null=True)
    week_start = DateTimeField(null=True)
    week_end = DateTimeField(null=True)
    created_by = ReferenceField(User, required=True)
    is_active = BooleanField(default=True, required=True)
    created_at = DateTimeField(default=datetime.utcnow)

    meta = {
        'collection': 'study_plans',
        'indexes': [
            'student_id',
            'created_by',
            'week_start',
            'week_end',
            'is_active',
            'created_at'
        ]
    }


class StudyPlanItem(Document):
    id = ObjectIdField(primary_key=True, default=ObjectId)
    plan_id = ReferenceField(StudyPlan, required=True)
    title = StringField(required=True, max_length=220)
    lesson_id = ReferenceField(Lesson, null=True)
    test_id = ReferenceField(Test, null=True)
    due_at = DateTimeField(null=True)
    is_done = BooleanField(default=False, required=True)
    done_at = DateTimeField(null=True)
    created_at = DateTimeField(default=datetime.utcnow)

    meta = {
        'collection': 'study_plan_items',
        'indexes': [
            'plan_id',
            'lesson_id',
            'test_id',
            'due_at',
            'is_done',
            'created_at'
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


class StudentFavoriteQuestion(Document):
    id = ObjectIdField(primary_key=True, default=ObjectId)
    student_id = ReferenceField(User, required=True)
    question_type = StringField(required=True, choices=["mcq", "interactive"])

    # Keep references when available.
    question_id = ReferenceField(Question, null=True)
    interactive_question_id = ReferenceField(TestInteractiveQuestion, null=True)

    # Snapshot for stable rendering even if original question changes/deletes.
    question_text = StringField(null=True)
    question_images = ListField(StringField(max_length=500), default=list)
    choices = ListField(EmbeddedDocumentField(Choice), default=list)
    correct_answer_text = StringField(null=True)
    correct_answer_image_url = StringField(max_length=500, null=True)
    difficulty = StringField(default="medium", choices=["easy", "medium", "hard"])
    created_at = DateTimeField(default=datetime.utcnow)

    meta = {
        'collection': 'student_favorite_questions',
        'indexes': [
            'student_id',
            'question_type',
            'question_id',
            'interactive_question_id',
            'created_at',
        ]
    }
