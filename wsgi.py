import os
import sys
from pathlib import Path

# For Heroku: add parent directory to path so package imports work
sys.path.insert(0, str(Path(__file__).parent.parent))

# Load environment variables
os.environ.setdefault('MONGODB_URI', 'mongodb+srv://Hussein:husseinDBpassword@edupath-cluster.jwgyihf.mongodb.net/study_platform?appName=edupath-cluster')
os.environ.setdefault('SECRET_KEY', 'your-strong-secret-key')
os.environ.setdefault('FLASK_ENV', 'production')

# Import from package
from study_platform.app import create_app

app = create_app()

if __name__ == "__main__":
    app.run()

