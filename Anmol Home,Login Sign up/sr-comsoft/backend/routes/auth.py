from flask import Blueprint, request, jsonify
import bcrypt
import jwt
import datetime
from config import JWT_SECRET
from models.db import get_db

auth_bp = Blueprint("auth", __name__)


@auth_bp.route("/api/register", methods=["POST"])
def register():
    data = request.get_json()
    name = data.get("name", "").strip()
    email = data.get("email", "").strip().lower()
    password = data.get("password", "")
    phone_number = data.get("phone_number", "").strip()
    country_code = data.get("country_code", "+91").strip()

    if not name or not email or not password:
        return jsonify({"error": "All fields are required"}), 400
    if len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters"}), 400

    password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

    try:
        conn = get_db()
        cursor = conn.cursor(buffered=True)
        cursor.execute(
            "INSERT INTO users (name, email, password_hash, phone_number, country_code) VALUES (%s, %s, %s, %s, %s)",
            (name, email, password_hash, phone_number or None, country_code),
        )
        conn.commit()
        user_id = cursor.lastrowid
        cursor.close()
        conn.close()

        token = jwt.encode(
            {"user_id": user_id, "exp": datetime.datetime.utcnow() + datetime.timedelta(days=7)},
            JWT_SECRET,
            algorithm="HS256",
        )
        return jsonify({"message": "Account created", "token": token}), 201

    except Exception as e:
        if "Duplicate entry" in str(e):
            return jsonify({"error": "Email already registered"}), 409
        return jsonify({"error": str(e)}), 500


@auth_bp.route("/api/login", methods=["POST"])
def login():
    data = request.get_json()
    email = data.get("email", "").strip().lower()
    password = data.get("password", "")

    try:
        conn = get_db()
        cursor = conn.cursor(dictionary=True, buffered=True)
        cursor.execute("SELECT * FROM users WHERE email = %s", (email,))
        user = cursor.fetchone()
        cursor.close()
        conn.close()

        if not user:
            return jsonify({"error": "Invalid email or password"}), 401

        if not bcrypt.checkpw(password.encode("utf-8"), user["password_hash"].encode("utf-8")):
            return jsonify({"error": "Invalid email or password"}), 401

        token = jwt.encode(
            {"user_id": user["id"], "exp": datetime.datetime.utcnow() + datetime.timedelta(days=7)},
            JWT_SECRET,
            algorithm="HS256",
        )
        return jsonify({
            "message": "Login successful",
            "token": token,
            "user": {
                "id": user["id"],
                "name": user["name"],
                "email": user["email"],
                "phone_number": user["phone_number"],
                "country_code": user["country_code"],
            },
        }), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500