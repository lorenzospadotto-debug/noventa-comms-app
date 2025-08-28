
import time
import requests
from typing import Optional, Tuple

# Returns (ok: bool, response)
def post_facebook(page_id: str, access_token: str, message: str, image_url: Optional[str] = None) -> Tuple[bool, str]:
    base = "https://graph.facebook.com/v20.0"
    try:
        if image_url:
            url = f"{base}/{page_id}/photos"
            payload = {"url": image_url, "caption": message, "access_token": access_token}
        else:
            url = f"{base}/{page_id}/feed"
            payload = {"message": message, "access_token": access_token}
        r = requests.post(url, data=payload, timeout=30)
        return (r.ok, r.text)
    except Exception as e:
        return (False, str(e))

def post_instagram(ig_user_id: str, access_token: str, caption: str, image_url: str) -> Tuple[bool, str]:
    base = "https://graph.facebook.com/v20.0"
    try:
        create = requests.post(
            f"{base}/{ig_user_id}/media",
            data={"image_url": image_url, "caption": caption, "access_token": access_token},
            timeout=30,
        )
        if not create.ok:
            return (False, create.text)
        container_id = create.json().get("id")
        time.sleep(2)
        publish = requests.post(
            f"{base}/{ig_user_id}/media_publish",
            data={"creation_id": container_id, "access_token": access_token},
            timeout=30,
        )
        return (publish.ok, publish.text)
    except Exception as e:
        return (False, str(e))

def post_linkedin(access_token: str, text: str, image_url: Optional[str] = None, org_id: Optional[str] = None) -> Tuple[bool, str]:
    try:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "X-Restli-Protocol-Version": "2.0.0",
            "Content-Type": "application/json",
        }
        author = org_id if org_id else "urn:li:person:me"
        payload = {
            "author": author,
            "lifecycleState": "PUBLISHED",
            "specificContent": {
                "com.linkedin.ugc.ShareContent": {
                    "shareCommentary": {"text": text},
                    "shareMediaCategory": "NONE",
                }
            },
            "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"},
        }
        r = requests.post("https://api.linkedin.com/v2/ugcPosts", headers=headers, json=payload, timeout=30)
        return (r.ok, r.text)
    except Exception as e:
        return (False, str(e))

def post_x(bearer_token: str, user_id: str, text: str) -> Tuple[bool, str]:
    try:
        headers = {"Authorization": f"Bearer {bearer_token}", "Content-Type": "application/json"}
        payload = {"text": text}
        r = requests.post("https://api.twitter.com/2/tweets", headers=headers, json=payload, timeout=30)
        return (r.ok, r.text)
    except Exception as e:
        return (False, str(e))
