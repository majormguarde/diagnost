from flask_wtf import FlaskForm
from wtforms import PasswordField, StringField, SubmitField
from wtforms.validators import DataRequired, EqualTo, Length


class RegisterForm(FlaskForm):
    phone = StringField("Телефон", validators=[DataRequired(), Length(min=6, max=32)])
    name = StringField("Имя", validators=[DataRequired(), Length(min=2, max=120)])
    password = PasswordField("Пароль", validators=[DataRequired(), Length(min=6, max=128)])
    password_confirm = PasswordField(
        "Повторите пароль", validators=[DataRequired(), EqualTo("password")]
    )
    submit = SubmitField("Зарегистрироваться")


class LoginForm(FlaskForm):
    phone = StringField("Телефон", validators=[DataRequired(), Length(min=6, max=32)])
    password = PasswordField("Пароль", validators=[DataRequired(), Length(min=1, max=128)])
    submit = SubmitField("Войти")
