from flask_wtf import FlaskForm
from wtforms import PasswordField, StringField, SubmitField
from wtforms.validators import DataRequired, EqualTo, Length, Email, Optional


class RegisterForm(FlaskForm):
    phone = StringField("Телефон", validators=[DataRequired(), Length(min=6, max=32)])
    name = StringField("Имя", validators=[DataRequired(), Length(min=2, max=120)])
    client_whatsapp = StringField(
        "WhatsApp (номер)",
        validators=[Optional(), Length(max=32)],
        description="Необязательно; если пусто — для чата используется телефон выше",
    )
    client_telegram = StringField(
        "Telegram (ник без @)",
        validators=[Optional(), Length(max=64)],
    )
    client_email = StringField("Email", validators=[Optional(), Email(), Length(max=120)])
    password = PasswordField("Пароль", validators=[DataRequired(), Length(min=6, max=128)])
    password_confirm = PasswordField(
        "Повторите пароль", validators=[DataRequired(), EqualTo("password")]
    )
    submit = SubmitField("Зарегистрироваться")


class LoginForm(FlaskForm):
    phone = StringField("Телефон или логин", validators=[DataRequired(), Length(min=1, max=32)])
    password = PasswordField("Пароль", validators=[DataRequired(), Length(min=1, max=128)])
    submit = SubmitField("Войти")
