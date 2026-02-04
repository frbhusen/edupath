import os
import sys
import types
import importlib.util
from pathlib import Path

# Ensure the app directory is importable
APP_DIR = Path(__file__).parent
sys.path.insert(0, str(APP_DIR))

# Load environment variables (Heroku config vars should provide these)
os.environ.setdefault("FLASK_ENV", "production")

# Create a virtual package so relative imports in app.py work
package_name = "study_platform"
package = types.ModuleType(package_name)
package.__path__ = [str(APP_DIR)]
sys.modules[package_name] = package

app_spec = importlib.util.spec_from_file_location(f"{package_name}.app", APP_DIR / "app.py")
app_module = importlib.util.module_from_spec(app_spec)
sys.modules[f"{package_name}.app"] = app_module
app_spec.loader.exec_module(app_module)

app = app_module.create_app()

if __name__ == "__main__":
    app.run()


