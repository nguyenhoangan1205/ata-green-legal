"""Gọi Facebook Graph API để quét và đăng bài lên các Trang mình quản lý.

Dùng cho tab "Đăng Trang bằng API" của FB Auto Poster. Cần: pip install requests
"""
import json
import os
import time

import requests

GRAPH_VERSION = "v23.0"
GRAPH = f"https://graph.facebook.com/{GRAPH_VERSION}"
GRAPH_VIDEO = f"https://graph-video.facebook.com/{GRAPH_VERSION}"

# Link lấy User Access Token (Graph API Explorer) và kiểm tra / gia hạn token.
REQUIRED_SCOPES = ["pages_show_list", "pages_read_engagement", "pages_manage_posts"]
TOKEN_URL = "https://developers.facebook.com/tools/explorer/"
DEBUG_TOKEN_URL = "https://developers.facebook.com/tools/debug/accesstoken/"

PHOTO_EXT = (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".heic")
VIDEO_EXT = (".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".wmv", ".3gp")


class GraphError(Exception):
    pass


def _check(resp):
    try:
        data = resp.json()
    except ValueError:
        raise GraphError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    if isinstance(data, dict) and "error" in data:
        err = data["error"]
        raise GraphError(f"{err.get('message', 'Lỗi không rõ')} (code {err.get('code')})")
    if resp.status_code >= 400:
        raise GraphError(f"HTTP {resp.status_code}: {data}")
    return data


def get_me(user_token):
    """{'id','name'} của tài khoản sở hữu token."""
    return _check(requests.get(f"{GRAPH}/me", params={"fields": "id,name", "access_token": user_token},
                               timeout=30))


def list_pages(user_token):
    """Tất cả Trang mà token quản lý: [{'id','name','link','access_token'}]."""
    pages, url = [], f"{GRAPH}/me/accounts"
    params = {"fields": "id,name,link,access_token", "limit": 100, "access_token": user_token}
    while url:
        data = _check(requests.get(url, params=params, timeout=30))
        for p in data.get("data", []):
            pages.append({"id": p["id"], "name": p.get("name", ""),
                          "link": p.get("link") or f"https://www.facebook.com/{p['id']}",
                          "access_token": p.get("access_token", "")})
        url = data.get("paging", {}).get("next")
        params = None  # link "next" đã chứa sẵn tham số
    return pages


def split_media(paths):
    photos = [p for p in paths if p.lower().endswith(PHOTO_EXT)]
    videos = [p for p in paths if p.lower().endswith(VIDEO_EXT)]
    return photos, videos


def upload_photo(page_id, page_token, path, published, caption=""):
    data = {"access_token": page_token, "published": "true" if published else "false"}
    if caption:
        data["caption"] = caption
    with open(path, "rb") as f:
        return _check(requests.post(f"{GRAPH}/{page_id}/photos", data=data, files={"source": f}, timeout=300))


def upload_video(page_id, page_token, path, description="", stop=None):
    """Tải video theo từng phần (start → transfer → finish), đăng được file lớn. Trả về id video."""
    url = f"{GRAPH_VIDEO}/{page_id}/videos"
    size = os.path.getsize(path)
    start = _check(requests.post(url, data={"access_token": page_token, "upload_phase": "start",
                                            "file_size": str(size)}, timeout=60))
    session_id = start["upload_session_id"]
    s, e = int(start["start_offset"]), int(start["end_offset"])
    with open(path, "rb") as f:
        while s < e:
            if stop and stop():
                raise GraphError("Đã dừng khi đang tải video")
            f.seek(s)
            chunk = f.read(e - s)
            for attempt in range(3):  # mạng chập chờn thì thử lại
                try:
                    r = _check(requests.post(url, data={
                        "access_token": page_token, "upload_phase": "transfer",
                        "upload_session_id": session_id, "start_offset": str(s)},
                        files={"video_file_chunk": ("chunk", chunk, "application/octet-stream")}, timeout=300))
                    break
                except (requests.RequestException, GraphError):
                    if attempt == 2:
                        raise
                    time.sleep(3)
            s, e = int(r["start_offset"]), int(r["end_offset"])
    _check(requests.post(url, data={"access_token": page_token, "upload_phase": "finish",
                                    "upload_session_id": session_id, "description": description}, timeout=120))
    return start["video_id"]


def post_to_page(page, message, media_paths=(), link="", stop=None):
    """Đăng 1 bài lên 1 Trang. Trả về (id_bài, ghi_chú).

    - Có video: đăng video đầu tiên kèm nội dung (API không cho trộn ảnh + video trong 1 bài).
    - 1 ảnh (không link): /photos.  Nhiều ảnh: tải ảnh ở chế độ chưa đăng rồi gộp vào 1 bài /feed.
    - Chỉ chữ / link: /feed.
    """
    page_id, token = page["id"], page.get("access_token")
    if not token:
        raise GraphError("Không có page token (tài khoản không còn quyền với Trang này?)")
    photos, videos = split_media(media_paths)

    if videos:
        skipped = len(videos) - 1 + len(photos) + bool(link)
        note = f" (bỏ qua {skipped} tệp/link: bài video chỉ đăng được 1 video)" if skipped else ""
        return upload_video(page_id, token, videos[0], message, stop), "Đã đăng video" + note

    if len(photos) == 1 and not link:
        r = upload_photo(page_id, token, photos[0], published=True, caption=message)
        return r.get("post_id") or r.get("id", ""), "Đã đăng"

    data = {"access_token": token}
    if message:
        data["message"] = message
    if link:
        data["link"] = link
    for i, path in enumerate(photos):
        if stop and stop():
            raise GraphError("Đã dừng khi đang tải ảnh")
        media = upload_photo(page_id, token, path, published=False)
        data[f"attached_media[{i}]"] = json.dumps({"media_fbid": media["id"]})
    if len(data) == 1:
        raise GraphError("Bài đăng trống: chưa có nội dung, link hay ảnh/video")
    r = _check(requests.post(f"{GRAPH}/{page_id}/feed", data=data, timeout=60))
    return r.get("id", ""), "Đã đăng"
