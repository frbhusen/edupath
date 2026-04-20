from flask_wtf import FlaskForm
from flask_wtf.file import FileField, FileAllowed
from wtforms import StringField, TextAreaField, PasswordField, SelectField, SubmitField, IntegerField, BooleanField
from wtforms.validators import DataRequired, Length, EqualTo, Optional, Regexp, ValidationError


def _strip_edges(value):
    return value.strip() if isinstance(value, str) else value

class RegisterForm(FlaskForm):
    first_name = StringField("الاسم الأول (باللغة العربية)", filters=[_strip_edges], validators=[DataRequired(), Length(min=3, max=20)])
    last_name = StringField("الكنية  (باللغة العربية)", filters=[_strip_edges], validators=[DataRequired(), Length(min=2, max=20)])
    username = StringField(
        "اسم المستخدم (بالإنجليزية فقط)", 
        filters=[_strip_edges],
        validators=[
            DataRequired(), 
            Length(min=3, max=80),
            Regexp(r'^[A-Za-z0-9_ ]+$', message="اسم المستخدم يجب أن يكون بالإنجليزية فقط (حروف/أرقام/_)"),
        ]
    )
    phone = StringField(
        "رقم الهاتف (10 أرقام يبدأ ب 09)", 
        validators=[
            DataRequired(),
            Length(min=10, max=10),
            Regexp(r'^09\d{8}$', message="يجب أن يكون رقم الهاتف 10 أرقام ويبدأ ب 09")
        ]
    )
    password = PasswordField("كلمة المرور (على الأقل 6 أحرف)", validators=[DataRequired(), Length(min=6)])
    confirm = PasswordField("تأكيد كلمة المرور", validators=[DataRequired(), EqualTo("password")])
    submit = SubmitField("تسجيل")

class LoginForm(FlaskForm):
    username_or_phone = StringField("اسم المستخدم أو رقم الهاتف", filters=[_strip_edges], validators=[DataRequired()])
    password = PasswordField("كلمة المرور", validators=[DataRequired()])
    submit = SubmitField("تسجيل الدخول")
    remember_me = BooleanField("تذكرني")

class SubjectForm(FlaskForm):
    name = StringField("الاسم", validators=[DataRequired(), Length(max=120)])
    description = TextAreaField("الوصف")
    banner_image_url = StringField("رابط بانر المادة (1600x900)", validators=[Optional(), Length(max=500)])
    requires_code = BooleanField("يتطلب رمز تفعيل")
    submit = SubmitField("حفظ")

class SectionForm(FlaskForm):
    title = StringField("العنوان", validators=[DataRequired(), Length(max=120)])
    description = TextAreaField("الوصف")
    requires_code = BooleanField("يتطلب رمز تفعيل")
    submit = SubmitField("حفظ")

class LessonForm(FlaskForm):
    title = StringField("العنوان", validators=[DataRequired(), Length(max=200)])
    content = TextAreaField("المحتوى", validators=[Optional()])
    requires_code = BooleanField("يتطلب رمز تفعيل")
    link_label = StringField("تسمية الرابط", validators=[Length(max=120)])
    link_url = StringField("رابط URL", validators=[Length(max=500)])
    link_label_2 = StringField("تسمية الرابط 2", validators=[Length(max=120)])
    link_url_2 = StringField("رابط URL 2", validators=[Length(max=500)])
    video_file = FileField('رفع فيديو الدرس (MP4)', validators=[
        FileAllowed(['mp4', 'webm'], 'فقط ملفات الفيديو مسموحة!')
    ])
    submit = SubmitField("حفظ")

class TestForm(FlaskForm):
    title = StringField("العنوان", validators=[DataRequired(), Length(max=200)])
    description = TextAreaField("الوصف")
    lesson_id = SelectField("الدرس المرتبط", coerce=str, validators=[Optional()])
    requires_code = BooleanField("يتطلب رمز تفعيل")
    submit = SubmitField("حفظ")

class QuestionForm(FlaskForm):
    text = TextAreaField("نص السؤال", validators=[DataRequired()])
    submit = SubmitField("إضافة سؤال")

class ChoiceForm(FlaskForm):
    text = StringField("نص الخيار", validators=[DataRequired(), Length(max=400)])
    is_correct = BooleanField("هل هو صحيح؟")
    submit = SubmitField("إضافة خيار")


class StudentEditForm(FlaskForm):
    first_name = StringField("الاسم الأول", validators=[DataRequired(), Length(min=2, max=80)])
    last_name = StringField("اسم العائلة", validators=[DataRequired(), Length(min=2, max=80)])
    username = StringField("اسم المستخدم", validators=[DataRequired(), Length(min=3, max=80)])
    phone = StringField("رقم الهاتف", validators=[DataRequired(), Length(min=7, max=20)])
    password_hash = StringField("كلمة مرور جديدة", validators=[Optional(), Length(min=6)])
    role = SelectField(
        "الدور",
        choices=[
            ("student", "طالب"),
            ("teacher", "معلم"),
            ("question_editor", "محرر أسئلة"),
            ("admin", "مشرف"),
        ],
        validators=[DataRequired()]
    )
    submit = SubmitField("حفظ")


class StudentProfileForm(FlaskForm):
    first_name = StringField("الاسم الأول", filters=[_strip_edges], validators=[DataRequired(), Length(min=2, max=80)])
    last_name = StringField("اسم العائلة", filters=[_strip_edges], validators=[DataRequired(), Length(min=2, max=80)])
    username = StringField(
        "اسم المستخدم (بالإنجليزية فقط)",
        filters=[_strip_edges],
        validators=[
            DataRequired(),
            Length(min=3, max=80),
            Regexp(r'^[A-Za-z0-9_ ]+$', message="اسم المستخدم يجب أن يكون بالإنجليزية فقط (حروف/أرقام/_)")
        ]
    )
    phone = StringField(
        "رقم الهاتف (10 أرقام يبدأ ب 09)",
        validators=[
            DataRequired(),
            Length(min=10, max=10),
            Regexp(r'^09\d{8}$', message="يجب أن يكون رقم الهاتف 10 أرقام ويبدأ ب 09")
        ]
    )
    current_password = PasswordField("كلمة المرور الحالية", validators=[Optional()])
    new_password = PasswordField("كلمة المرور الجديدة", validators=[Optional(), Length(min=6)])
    confirm_new_password = PasswordField("تأكيد كلمة المرور الجديدة", validators=[Optional(), EqualTo("new_password")])
    submit = SubmitField("حفظ التعديلات")


class ActivationForm(FlaskForm):
    code = StringField("رمز التفعيل", validators=[DataRequired(), Length(min=6, max=6)])
    submit = SubmitField("تفعيل")

class LessonActivationForm(FlaskForm):
    code = StringField("رمز تفعيل الدرس", validators=[DataRequired(), Length(min=6, max=6)])
    submit = SubmitField("تفعيل الدرس")
