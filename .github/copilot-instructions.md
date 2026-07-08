# EduPath AI Editing Instructions

Use these instructions every time you work in this repository.

## 1) Understand the project before editing
- Main backend: Flask + MongoEngine (Python).
- Entry point: `/home/runner/work/edupath/edupath/app.py` (`create_app()`).
- Core modules:
  - `/home/runner/work/edupath/edupath/auth.py` (authentication/session flow)
  - `/home/runner/work/edupath/edupath/admin.py` (admin routes)
  - `/home/runner/work/edupath/edupath/teacher.py` (teacher workflows)
  - `/home/runner/work/edupath/edupath/student.py` (student workflows)
  - `/home/runner/work/edupath/edupath/models.py` (MongoEngine documents)
  - `/home/runner/work/edupath/edupath/permissions.py` (role/scope checks)
- UI:
  - Server-rendered Jinja templates in `/home/runner/work/edupath/edupath/templates`
  - Static assets in `/home/runner/work/edupath/edupath/static`
- Mobile wrapper:
  - Expo React Native app in `/home/runner/work/edupath/edupath/mobile_app`

## 2) Validate changes with project-native commands
- Install backend dependencies: `pip install -r /home/runner/work/edupath/edupath/requirements.txt`
- Run backend tests: `python -m unittest discover -s /home/runner/work/edupath/edupath/tests`
- For mobile app changes:
  - `cd /home/runner/work/edupath/edupath/mobile_app && npm install`
  - `cd /home/runner/work/edupath/edupath/mobile_app && npm run start`

## 3) Editing rules
- Keep edits minimal and scoped to the request.
- Reuse existing patterns in the touched module.
- Do not change unrelated files.
- Do not introduce secrets.
- Preserve role-based access and subject-scope restrictions when touching auth/permissions/routes.

## 4) Mandatory change tracking (always update this file)
Whenever any code file is edited, append one new line in **Edit Log** below before finishing work.

Required format:
- `YYYY-MM-DD | <changed file paths> | <short reason>`

Example:
- `2026-07-08 | /home/runner/work/edupath/edupath/teacher.py | Added scoped permission check for assignment route`

## 5) Edit Log
- 2026-07-08 | /home/runner/work/edupath/edupath/.github/copilot-instructions.md | Added AI instructions and mandatory per-edit logging process
- 2026-07-08 | /home/runner/work/edupath/edupath/templates/index.html, /home/runner/work/edupath/edupath/static/style.css | Moved homepage inline styles to shared stylesheet and applied small homepage UI polish classes
