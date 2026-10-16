"""Gọi Facebook Graph API để quét và đăng bài lên các Trang mình quản lý.

Không phụ thuộc giao diện, dùng được độc lập hoặc từ fb_pages_tab.py.
Cần: pip install requests
"""
import os

import requests

GRAPH_VERSION = "v23.0"
GRAPH = f"https://graph.facebook.com/{GRAPH_VERSION}"
GRAPH_VIDEO = f"https://graph-video.facebook.com/{GRAPH_VERSION}"

# Link để lấy User Access Token (Graph API Explorer) với đủ quyền cần thiết.
REQUIRED_SCOPES = ["pages_show_list", "pages_read_engagement", "pages_manage_posts"]
TOKEN_URL = "https://developers.facebook.com/tools/explorer/"
DEBUG_TOKEN_URL = "https://developers.facebook.com/tools/debug/accesstoken/"

PHOTO_EXT = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".heic"}
VIDEO_EXT = {".mp4", ".mov", ".avi", ".mkv", ".m4v", ".wmv", ".webm", ".3gp"}

TIMEOUT = 60
UPLOAD_TIMEOUT = 600


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
    """Trả về {'id','name'} của tài khoản sở hữu token."""
    return _check(requests.get(f"{GRAPH}/me", params={
        "fields": "id,name", "access_token": user_token}, timeout=TIMEOUT))


def list_pages(user_token):
    """Quét tất cả Trang mà token quản lý (tự lật trang kết quả).

    Mỗi phần tử: {'id','name','link','access_token'}.
    """
    pages = []
    url = f"{GRAPH}/me/accounts"
    params = {"fields": "id,name,link,access_token", "limit": 100,
              "access_token": user_token}
    while url:
        data = _check(requests.get(url, params=params, timeout=TIMEOUT))
        for p in data.get("data", []):
            pages.append({
                "id": p["id"],
                "name": p.get("name", ""),
                "link": p.get("link") or f"https://facebook.com/{p['id']}",
                "access_token": p.get("access_token", ""),
            })
        url = data.get("paging", {}).get("next")
        params = None  # link 'next' đã chứa đủ tham số
    return pages


def split_media(paths):
    photos, videos = [], []
    for p in paths:
        ext = os.path.splitext(p)[1].lower()
        if ext in VIDEO_EXT:
            videos.append(p)
        elif ext in PHOTO_EXT:
            photos.append(p)
    return photos, videos


def _upload_photo(page_id, page_token, path, published, caption=""):
    with open(path, "rb") as f:
        data = {"access_token": page_token, "published": "true" if published else "false"}
        if caption:
            data["caption"] = caption
        return _check(requests.post(f"{GRAPH}/{page_id}/photos", data=data,
                                    files={"source": f}, timeout=UPLOAD_TIMEOUT))


def _upload_video(page_id, page_token, path, description):
    with open(path, "rb") as f:
        return _check(requests.post(f"{GRAPH_VIDEO}/{page_id}/videos", data={
            "access_token": page_token, "description": description},
            files={"source": f}, timeout=UPLOAD_TIMEOUT))


def post_to_page(page, message, media_paths=(), link=""):
    """Đăng 1 bài lên 1 Trang. Trả về (post_id, ghi_chú).

    - Chỉ chữ / chữ + link: /feed
    - 1 ảnh: /photos (caption = nội dung)
    - Nhiều ảnh: tải từng ảnh ở chế độ chưa đăng rồi gộp vào 1 bài /feed
    - Có video: đăng video đầu tiên kèm nội dung (Facebook không cho trộn ảnh + video
      trong 1 bài qua API); ảnh và các video còn lại bị bỏ qua, có ghi chú.
    """
    page_id, token = page["id"], page["access_token"]
    if not token:
        raise GraphError("Trang không có page access token (thiếu quyền?)")
    photos, videos = split_media(media_paths)
    note = ""

    if videos:
        res = _upload_video(page_id, token, videos[0], message)
        skipped = len(videos) - 1 + len(photos)
        if skipped:
            note = f"Đã bỏ qua {skipped} tệp (API chỉ đăng 1 video/bài, không trộn ảnh)"
        return res.get("id", ""), note

    if len(photos) == 1:
        res = _upload_photo(page_id, token, photos[0], published=True, caption=message)
        return res.get("post_id") or res.get("id", ""), note

    data = {"access_token": token}
    if message:
        data["message"] = message
    if link:
        data["link"] = link
    for i, path in enumerate(photos):
        media = _upload_photo(page_id, token, path, published=False)
        data[f"attached_media[{i}]"] = '{"media_fbid":"%s"}' % media["id"]
    if len(data) == 1:
        raise GraphError("Bài đăng trống: chưa có nội dung, link hay ảnh/video")
    res = _check(requests.post(f"{GRAPH}/{page_id}/feed", data=data, timeout=TIMEOUT))
    return res.get("id", ""), note
