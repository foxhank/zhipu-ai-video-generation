import smtplib
import sqlite3
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.header import Header
from apscheduler.schedulers.background import BackgroundScheduler
from zhipuai import ZhipuAI
from zhipuai.core._errors import APIRequestFailedError
from flask_cors import CORS
from flask import Flask, render_template, request, redirect, jsonify
import configparser

app = Flask(__name__)
CORS(app)



# 读取配置文件
config = configparser.ConfigParser()
config.read('config.ini')

# 获取API密钥
api_key = config['DEFAULT']['api_key']

# 获取邮件提醒配置
email_enabled = config['email']['enable']
email_host = config['email']['email_host']
email_port = int(config['email']['email_port'])
email_user = config['email']['email_user']
email_pass = config['email']['email_pass']

# 创建客户端实例
client = ZhipuAI(api_key=api_key)


def get_db_connection():
    conn = sqlite3.connect('tasks.db', check_same_thread=False)
    cursor = conn.cursor()
    return conn, cursor


def init_db():
    conn, cursor = get_db_connection()
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS video_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        prompt TEXT NOT NULL,
        with_audio BOOLEAN NOT NULL,
        image_url TEXT,
        duration INTEGER,
        fps INTEGER,
        email TEXT,
        status INTEGER DEFAULT 0,
        task_id TEXT,
        video_url TEXT DEFAULT NULL,
        cover_url TEXT DEFAULT NULL
    )
    ''')
    conn.commit()
    conn.close()





def send_email(to_email, subject, body):
    msg = MIMEMultipart()
    msg['From'] = '视频生成提醒'
    msg['To'] = to_email
    msg['Subject'] = subject

    msg.attach(MIMEText(body, 'plain'))

    try:
        server = smtplib.SMTP_SSL(email_host, email_port)
        server.login(email_user, email_pass)
        text = msg.as_string()
        server.sendmail(email_user, to_email, text)
        server.quit()
        print(f"Email sent to {to_email}")
    except Exception as e:
        print(f"Failed to send email: {e}")


@app.route('/generate', methods=['POST'])
def generate():
    data = request.get_json()
    prompt = data.get('prompt')
    with_audio = data.get('with_audio')
    image_url = data.get('image_url')
    duration = data.get('duration')
    fps = data.get('fps')
    email = data.get('email')

    if not prompt and not image_url:
        return jsonify({"error": "Missing required fields"}), 400

    if image_url:
        response = client.videos.generations(
            model="cogvideox-flash",
            image_url=image_url,
            prompt="prompt",
            with_audio=with_audio,
        )
    else:
        response = client.videos.generations(
            model="cogvideox-flash",
            prompt=prompt,
            with_audio=with_audio,
            size="1920x1080",
            duration=duration,
            fps=fps
        )

    response_id = response.id
    with_audio_int = 1 if with_audio else 0
    conn, cursor = get_db_connection()
    cursor.execute('''
    INSERT INTO video_requests (prompt, with_audio, image_url, duration, fps, email, task_id)
    VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (prompt, with_audio_int, image_url, duration, fps, email, response_id))
    conn.commit()
    conn.close()

    result = {
        "status": "success",
        "id": response_id,
    }
    return jsonify(result), 200


@app.route('/', methods=["GET"])
def index():
    return render_template('index.html')


@app.route('/check/<task_id>', methods=['GET'])
def check(task_id):
    try:
        response = client.videos.retrieve_videos_result(id=task_id)
        status = response.task_status
        if status == "SUCCESS":
            for video in response.video_result:
                url = video.url
                cover_image_url = video.cover_image_url
            conn, cursor = get_db_connection()
            cursor.execute('''
            UPDATE video_requests SET status = 1, video_url = ?, cover_url = ?
            WHERE task_id = ?
            ''', (url, cover_image_url, task_id))
            conn.commit()
            conn.close()
            return jsonify(
                {
                    "status": "success",
                    "url": url,
                    "cover_image_url": cover_image_url
                }
            ), 200
        elif status == "FAIL":
            conn, cursor = get_db_connection()
            cursor.execute('''
            UPDATE video_requests SET status = 2
            WHERE task_id = ?
            ''', (task_id,))  # 确保这里是一个元组
            conn.commit()
            conn.close()
            return jsonify(
                {
                    "status": "failed",
                }
            ), 200
        elif status == "PROCESSING":
            return jsonify(
                {
                    "status": "processing",
                }
            ), 200
        else:
            return jsonify({"status": "failed"}), 200
    except APIRequestFailedError as e:
        conn, cursor = get_db_connection()
        cursor.execute('''
        UPDATE video_requests SET status = 2
        WHERE task_id = ?
        ''', (task_id,))  # 确保这里是一个元组
        conn.commit()
        conn.close()
        return jsonify({
            "status": "failed",
            "message": "视频生成请求可能包含敏感内容，请检查您的输入并再次尝试。"
        }), 200


def check_pending_requests():
    conn, cursor = get_db_connection()
    cursor.execute('SELECT * FROM video_requests WHERE status = 0')
    rows = cursor.fetchall()
    conn.close()

    for row in rows:
        try:
            response = client.videos.retrieve_videos_result(id=row[8])
            if response.task_status == "SUCCESS":  # 修改这里
                for video in response.video_result:
                    url = video.url
                    cover_image_url = video.cover_image_url
                conn, cursor = get_db_connection()
                cursor.execute('''
                UPDATE video_requests SET status = 1, video_url = ?, cover_url = ?
                WHERE id = ?
                ''', (url, cover_image_url, row[0]))
                conn.commit()
                conn.close()
                if row[6] and email_enabled:  # Check if email is provided
                    send_email(row[6], "[提醒]视频生成成功",
                               f"尊敬的用户，\n\n"
                               f"您提交的视频生成任务已经成功完成。\n\n"
                               f"**任务详情**:\n"
                               f"- **任务ID**: {row[8]}\n"
                               f"- **视频链接**: {url}\n"
                               f"- **封面图片链接**: {cover_image_url}\n\n"
                               f"祝好，\n")

            elif response.task_status == "FAIL":  # 修改这里
                conn, cursor = get_db_connection()
                cursor.execute('UPDATE video_requests SET status = 2 WHERE id = ?', (row[0],))
                conn.commit()
                conn.close()
                if row[6] and email_enabled:  # Check if email is provided
                    send_email(row[6], "[提醒]视频生成失败",
                               f"尊敬的用户，\n\n"
                               f"您提交的视频生成任务未能成功完成。\n\n"
                               f"**任务详情**:\n"
                               f"- **任务ID**: {row[8]}\n"
                               f"可能是由于存在敏感词汇或系统错误，请尝试重试。\n\n"
                               f"祝好，\n")
        except APIRequestFailedError:
            conn, cursor = get_db_connection()
            cursor.execute('UPDATE video_requests SET status = 2 WHERE id = ?', (row[0],))
            conn.commit()
            conn.close()
            if row[6] and email_enabled:  # Check if email is provided
                send_email(row[6], "[提醒]视频生成失败",
                           f"尊敬的用户，\n\n"
                           f"您提交的视频生成任务未能成功完成。\n\n"
                           f"**任务详情**:\n"
                           f"- **任务ID**: {row[0]}\n"
                           f"可能是由于存在敏感词汇或系统错误，请尝试重试。\n\n"
                           f"祝好，\n")


if __name__ == '__main__':
    init_db()
    scheduler = BackgroundScheduler()
    scheduler.add_job(func=check_pending_requests, trigger='interval', minutes=1)
    scheduler.start()
    app.run(port=4781)
