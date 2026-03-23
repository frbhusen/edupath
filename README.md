# Study Platform (Flask)

A simple study platform where teachers manage subjects, sections, lessons, and MCQ tests; students take tests and see their marks.

## Features
- Register/login with roles: teacher or student
- Teacher: create/edit subjects, sections, lessons, tests, questions and choices
- Student: browse content, take MCQ tests, view scores
- Friendly Duels between students with invite links, synchronized timers, and live notifications
- Duel rewards and gamification: XP entry fee, win/loss settlement, perfect-score bonuses, and XP change popup
- Post-duel review page showing questions, images, your answer, opponent answer, and correct answer
- Certificates system with issuance and verification flow
- Leaderboards for student progression and duel rankings
- Frequently Wrong review to focus on commonly missed questions
- Pinned Q&A section (Arabic: "الأسئلة والأجوبة المثبتة") for highlighted discussions
- Assignments workflow for publishing, solving, and submission tracking
- SQLite database file stored locally

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

### 3) Run the app
```powershell
python -m study_platform.app
```
Then open http://127.0.0.1:5000

## Notes
- Default `SECRET_KEY` is for development only. Set `SECRET_KEY` env var in production.
- Database path: `study_platform/study.db`. Delete it to reset the DB.
