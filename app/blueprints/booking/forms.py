from flask_wtf import FlaskForm
from wtforms import DateField, IntegerField, SelectField, SelectMultipleField, StringField, SubmitField, TextAreaField
from wtforms.validators import DataRequired, Optional


class BookingStep1Form(FlaskForm):
    master_id = SelectField("Мастер", coerce=int, validators=[DataRequired()])
    date = DateField("Дата", validators=[DataRequired()])
    
    # Данные автомобиля
    car_make = StringField("Марка автомобиля", validators=[DataRequired(message="Введите марку авто")])
    car_model = StringField("Модель", validators=[DataRequired(message="Введите модель")])
    car_year = IntegerField("Год выпуска", validators=[Optional()])
    car_number = StringField("Гос.номер", validators=[Optional()])
    
    # Описание проблемы (вместо выбора работ)
    problem_description = TextAreaField("Описание неисправностей", validators=[DataRequired(message="Опишите проблему")])
    
    submit = SubmitField("Показать свободное время")


class BookingConfirmForm(FlaskForm):
    start_slot_id = SelectField("Время начала", coerce=int, validators=[DataRequired()])
    submit = SubmitField("Подать заявку")
