import os
import sys
import json
import requests

def get_token(client_id, client_secret):
    url = 'https://oauth2.dailymotion.com/v2/token'
    data = {
        'grant_type': 'client_credentials',
        'client_id': client_id,
        'client_secret': client_secret
    }
    r = requests.post(url, data=data)
    r.raise_for_status()
    return r.json()['access_token']

def send_telegram_notification(platform_name, video_title, video_url, remaining_count):
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not bot_token or not chat_id:
        return
    import html
    escaped_title = html.escape(video_title or "Untitled")
    if remaining_count > 0:
        queue_info = f"📦 <b>Queue:</b> {remaining_count} video(s) remaining"
    else:
        queue_info = "🏁 <b>Queue:</b> All videos posted! Queue is empty."

    lines = [
        f"🚀 <b>{platform_name} Upload Confirmed!</b>\n",
        f"🎬 <b>Title:</b> {escaped_title}",
        f"🔗 <b>Link:</b> <a href=\"{video_url}\">{video_url}</a>",
        queue_info
    ]
    message = "\n".join(lines)
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": False
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Failed to send telegram alert: {e}")

def main():
    client_id = os.environ.get("DM_CLIENT_ID")
    client_secret = os.environ.get("DM_CLIENT_SECRET")
    profile_id = os.environ.get("DM_PROFILE_ID", "x6ag6dq")
    
    if not client_id or not client_secret:
        print("Missing DM_CLIENT_ID or DM_CLIENT_SECRET in environment!")
        sys.exit(1)

    upload_dir = 'uploaded'
    if not os.path.exists(upload_dir):
        print("No uploaded directory found.")
        return
        
    files = os.listdir(upload_dir)
    mp4s = [f for f in files if f.endswith('.mp4')]
    
    if not mp4s:
        print("Queue is empty. No videos to upload.")
        return
        
    # Just upload the first one in the queue
    mp4_file = sorted(mp4s)[0]
    base_name = mp4_file.replace('.mp4', '')
    
    mp4_path = os.path.join(upload_dir, mp4_file)
    json_path = os.path.join(upload_dir, base_name + '.json')
    thumb_path = os.path.join(upload_dir, base_name + '_thumb.jpg')
    
    title = base_name[:255]
    if os.path.exists(json_path):
        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                metadata = json.load(f)
            title = metadata.get("title", title)[:255]
        except Exception as e:
            print(f"Error reading metadata: {e}")

    print(f"Starting upload for {mp4_file} to Dailymotion...")
    try:
        access_token = get_token(client_id, client_secret)
        headers = {'Authorization': f'Bearer {access_token}'}

        # 1. Get upload URL
        session_resp = requests.post('https://api.dailymotion.com/v2/files/upload_sessions', headers=headers)
        session_resp.raise_for_status()
        upload_url = session_resp.json()['upload_url']
        
        # 2. Upload file
        with open(mp4_path, "rb") as f:
            upload_resp = requests.post(upload_url, files={"file": f})
        upload_resp.raise_for_status()
        file_url = upload_resp.json().get("url")
        
        if not file_url:
            print("Failed to get file_url from upload response")
            sys.exit(1)

        # 3. Create Video
        payload = {
            "title": title,
            "published": True,
            "category": "lifestyle",
            "visibility": "public",
            "is_for_kids": False,
            "source": {
                "file_url": file_url
            }
        }
        video_resp = requests.post(f"https://api.dailymotion.com/v2/profiles/{profile_id}/videos", headers=headers, json=payload)
        video_resp.raise_for_status()
        video_id = video_resp.json().get('id')
        print(f"Success! Dailymotion video created: {video_id}")
        
        # Cleanup
        os.remove(mp4_path)
        if os.path.exists(json_path): os.remove(json_path)
        if os.path.exists(thumb_path): os.remove(thumb_path)
        print(f"Cleaned up local files for {base_name}.")
        
        remaining = len(mp4s) - 1
        video_url = f"https://www.dailymotion.com/video/{video_id}"
        send_telegram_notification(
            platform_name="Dailymotion",
            video_title=title,
            video_url=video_url,
            remaining_count=remaining
        )
        
    except requests.exceptions.RequestException as e:
        print(f"API Error uploading {mp4_file}: {e}")
        if hasattr(e, 'response') and e.response is not None:
            print(e.response.text)
        sys.exit(1)
    except Exception as e:
        print(f"Unexpected error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
