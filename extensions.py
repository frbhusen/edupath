from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager

# Flask extensions initialized here to avoid circular imports

db = SQLAlchemy()
login_manager = LoginManager()
