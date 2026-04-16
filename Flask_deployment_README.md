# 🚀 Flask Deployment on cPanel

This project demonstrates how to deploy a Flask application on cPanel using Python App (Passenger WSGI).

---

## 📁 Project Structure

```
project/
│
├── app.py
├── passenger_wsgi.py
├── requirements.txt
└── templates/
```

---

## ⚙️ Setup Instructions

### 1️⃣ Create Flask App (`app.py`)

```python
from flask import Flask

app = Flask(__name__)

@app.route("/")
def home():
    return "Flask is Working on cPanel!"
```

---

### 2️⃣ Configure WSGI (`passenger_wsgi.py`)

```python
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from app import app as application
```

---

### 3️⃣ Add Dependencies (`requirements.txt`)

```
Flask==2.3.3
```

---

## 🌐 Deployment Steps (cPanel)

1. Go to **cPanel → Python App → Create Application**
2. Fill configuration:

| Field            | Value                       |
| ---------------- | --------------------------- |
| Python Version   | 3.11                        |
| Application Root | `yourdomain.com/yourfolder` |
| Application URL  | `/`                         |
| Startup File     | `passenger_wsgi.py`         |
| Entry Point      | `application`               |

---

### 4️⃣ Upload Files

Upload your project to:

```
/home/username/yourdomain.com/yourfolder
```

---

### 5️⃣ Add Requirements File

In cPanel:

* Go to **Configuration Files**
* Add:

```
requirements.txt
```

---

### 6️⃣ Install Dependencies

Click:

```
Run Pip Install
```

---

### 7️⃣ Restart Application

Click:

```
Restart App
```

---

## ✅ Verify Deployment

Open your browser:

```
https://yourdomain.com/
```

Expected output:

```
Flask is Working on cPanel!
```

---

## ❗ Common Issues & Fixes

| Issue                | Fix                              |
| -------------------- | -------------------------------- |
| 500 Error            | Check `stderr.log`               |
| ModuleNotFoundError  | Install dependencies             |
| Wrong WSGI config    | Fix `passenger_wsgi.py`          |
| Pip Install disabled | Add `requirements.txt` in config |
| Infinite loop error  | Remove `imp.load_source`         |

---

## 🔥 Important Notes

* Do NOT use `imp.load_source`
* Do NOT use "Run Script" for pip install
* Do NOT click "Create" again after setup
* Always restart app after changes

---

## 📌 Summary

```
Upload → Create App → Fix WSGI → Add requirements → Install → Restart → Done
```

---

## 🚀 Next Improvements

* Add HTML templates (`render_template`)
* Build REST APIs
* Connect database (MySQL / SQLite)
* Deploy full production app

---

## 👨‍💻 Author

Devendra Shakya
