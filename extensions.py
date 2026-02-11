from mongoengine import connect
from pymongo.errors import PyMongoError
from flask_login import LoginManager
from flask_caching import Cache

# Flask extensions initialized here to avoid circular imports

login_manager = LoginManager()
cache = Cache()

def init_mongo(app):
    """Initialize MongoDB connection"""
    try:
        client = connect(host=app.config['MONGODB_URI'])
        # Verify connection with a ping
        client.admin.command("ping")
        app.logger.info("MongoDB connection: OK")
    except PyMongoError as exc:
        app.logger.error(f"MongoDB connection: FAILED - {exc}")
    except Exception as exc:
        app.logger.error(f"MongoDB connection: FAILED - {exc}")
