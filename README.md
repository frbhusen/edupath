# EduPath (Flask)

An Arabic study platform (https://www.edu-path.app). Staff manage subjects, sections, lessons and tests; students unlock content with activation codes, take tests, earn XP and duel each other.

## Features
- Roles: admin, teacher and question editor (scoped to assigned subjects), student
- Teacher: create/edit subjects, sections, lessons, tests, questions and choices
- Student: browse content, take MCQ tests, view scores
- Friendly Duels between students with invite links, synchronized timers, and live notifications
- Duel rewards and gamification: Gems entry fee, win/loss settlement, perfect-score bonuses, and Gems change popup
- Post-duel review page showing questions, images, your answer, opponent answer, and correct answer
- Certificates system with issuance and verification flow
- Leaderboards for student progression and duel rankings
- Frequently Wrong review to focus on commonly missed questions
- Pinned Q&A section (Arabic: "الأسئلة والأجوبة المثبتة") for highlighted discussions
- Assignments workflow for publishing, solving, and submission tracking
- MongoDB (MongoEngine); Redis for cache and rate limits when configured

## Setup

### 1) Create a virtual environment (optional but recommended)
```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### 2) Install dependencies
```powershell
pip install -r requirements.txt
```

### 3) Configure
Put these in a `.env` file in the folder that contains `study_platform` (or set them as environment variables):
- `MONGODB_URI` (default `mongodb://localhost:27017/study_platform`)
- `SECRET_KEY` (required in production; the app logs a critical error if it is missing)
- Optional: `CACHE_REDIS_URL` / `REDIS_URL`, `SESSION_COOKIE_SECURE=true` behind HTTPS, `LOG_LEVEL`

### 4) Run the app
The repo folder must be named `study_platform` (the code uses package-relative imports). From the folder that contains it:
```powershell
python -m study_platform.app
```
Then open http://127.0.0.1:5000

## Tests
From the folder that contains `study_platform`:
```bash
pip install -r study_platform/requirements.txt -r study_platform/requirements-dev.txt
python -m unittest discover -s study_platform/tests -t .
```
The tests use an in-memory MongoDB (mongomock), so no database server is needed. GitHub Actions runs them on every push to `main` and on pull requests.

## Deploy
See `DEPLOY_TUTORIAL.md` (Ubuntu, Gunicorn behind Nginx, systemd service `edupath`).
