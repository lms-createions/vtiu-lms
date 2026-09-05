import os
from sqlalchemy import inspect
from app import app
from utils.extensions import db

print("Current directory:", os.getcwd())
print("Database configured:", bool(app.config.get('SQLALCHEMY_DATABASE_URI')))

with app.app_context():
    database_inspector = inspect(db.engine)
    tables = database_inspector.get_table_names()
    print("Tables:", tables)

    if "message" in tables:
        columns = database_inspector.get_columns("message")
        print("Message table columns:", [column["name"] for column in columns])
    else:
        print("Message table columns: table does not exist")
