from flask_wtf import FlaskForm
from wtforms import DateField, IntegerField, SelectField, SelectMultipleField, StringField, SubmitField, TextAreaField
from wtforms.validators import DataRequired, InputRequired, Length, Optional
from wtforms import ValidationError


class BookingStep1Form(FlaskForm):
    master_id = SelectField("Мастер", coerce=int, validators=[DataRequired()])
    date = DateField("Дата", validators=[DataRequired()])
    
    # Данные автомобиля
    car_make_id = SelectField("Марка автомобиля", coerce=int, validators=[InputRequired()])
    car_make_custom = StringField("Марка автомобиля", validators=[Optional()])
    car_model = StringField("Модель", validators=[DataRequired(message="Введите модель")])
    car_year = IntegerField("Год выпуска", validators=[Optional()])
    car_number = StringField("Гос.номер", validators=[Optional()])
    win_number = StringField("WIN номер", validators=[Optional(), Length(max=32)])
    
    # Описание проблемы (вместо выбора работ)
    problem_description = TextAreaField("Описание неисправностей", validators=[DataRequired(message="Опишите проблему")])
    
    submit = SubmitField("Показать свободное время")

    def validate_car_make_custom(self, field):
        if int(self.car_make_id.data or 0) == 0:
            value = (field.data or "").strip()
            if not value:
                raise ValidationError("Введите марку авто")


class BookingConfirmForm(FlaskForm):
    start_slot_id = SelectField("Время начала", coerce=int, validators=[DataRequired()])
    submit = SubmitField("Подать заявку")
