from flask_wtf import FlaskForm
from wtforms import StringField, TextAreaField, PasswordField, SelectField, SubmitField, IntegerField, BooleanField
from wtforms.validators import DataRequired, Length, EqualTo, Optional, Regexp, ValidationError

class RegisterForm(FlaskForm):
    first_name = StringField("الاسم الأول", validators=[DataRequired(), Length(min=2, max=80)])
    last_name = StringField("اسم العائلة", validators=[DataRequired(), Length(min=2, max=80)])
    username = StringField(
        "اسم المستخدم (بالإنجليزية فقط)", 
        validators=[
            DataRequired(), 
            Length(min=3, max=80),
            Regexp(r'^[A-Za-z0-9_]+$', message="اسم المستخدم يجب أن يكون بالإنجليزية فقط (حروف/أرقام/_)"),
        ]
    )
    phone = StringField(
        "رقم الهاتف (10 أرقام يبدأ ب 09)", 
        validators=[
            DataRequired(),
            Regexp(r'^09\d{8}$', message="يجب أن يكون رقم الهاتف 10 أرقام ويبدأ ب 09")
        ]
    )
    password = PasswordField("كلمة المرور (على الأقل 6 أحرف)", validators=[DataRequired(), Length(min=6)])
    confirm = PasswordField("تأكيد كلمة المرور", validators=[DataRequired(), EqualTo("password")])
    submit = SubmitField("تسجيل")

class LoginForm(FlaskForm):
    username_or_phone = StringField("اسم المستخدم أو رقم الهاتف", validators=[DataRequired()])
    password = PasswordField("كلمة المرور", validators=[DataRequired()])
    submit = SubmitField("تسجيل الدخول")

class SubjectForm(FlaskForm):
    name = StringField("الاسم", validators=[DataRequired(), Length(max=120)])
    description = TextAreaField("الوصف")
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
    username = StringField("اسم المستخدم", validators=[DataRequired(), Length(min=3, max=80)])
    phone = StringField("رقم الهاتف", validators=[DataRequired(), Length(min=7, max=20)])
    password = PasswordField("كلمة مرور جديدة", validators=[Optional(), Length(min=6)])
    role = SelectField("الدور", choices=[("student", "طالب"), ("teacher", "معلم")], validators=[DataRequired()])
    submit = SubmitField("حفظ")


class ActivationForm(FlaskForm):
    code = StringField("رمز التفعيل", validators=[DataRequired(), Length(min=6, max=6)])
    submit = SubmitField("تفعيل")

class LessonActivationForm(FlaskForm):
    code = StringField("رمز تفعيل الدرس", validators=[DataRequired(), Length(min=6, max=6)])
    submit = SubmitField("تفعيل الدرس")
