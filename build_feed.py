#!/usr/bin/env python3
"""配信フィード(streams.json)を生成する。

GitHub Actions の定期ジョブから実行する。**このスクリプトだけが YouTube の
APIキーに触れる。** キーはサーバ側の秘密として GitHub Secrets に置き、
配布するアプリには一切入らない。クライアントは出来上がった静的JSONを
読むだけなので、ユーザーが何人いてもAPIのクォータは増えない。

クォータの考え方:
  - チャンネル一覧の取得は **RSS**(youtube.com/feeds/videos.xml)。
    キー不要・クォータ消費ゼロ。ここで最近の動画IDだけを集める。
  - ライブ状態と予定開始時刻は **videos.list**。50件まとめて **1ユニット**。
  - search.list は使わない。1回100ユニットに加え、2026年6月の変更で
    新規プロジェクトは1日100回に制限されており、定期ポーリングには使えない。
  → 5チャンネル/15動画なら1回あたり2ユニット程度。1日144回動かしても
    300ユニット弱で、既定枠10,000に対して十分な余裕がある。

依存はPython標準ライブラリのみ。pip install を挟まないぶん、
ジョブが壊れる原因を1つ減らせる。

使い方:
  YOUTUBE_API_KEY=... python build_feed.py --channels channels.json --out streams.json
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ElementTree
from datetime import datetime, timezone

RSS_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
VIDEOS_API_URL = "https://www.googleapis.com/youtube/v3/videos"
# RSS の <yt:videoId> は完全修飾名で引く。名前空間の別名解決を挟まないぶん、
# ElementTree の版差に左右されない。
VIDEO_ID_TAG = "{http://www.youtube.com/xml/schemas/2015}videoId"

# videos.list が1回で受け付けるIDの上限。これを超えると分割して呼ぶ。
MAX_IDS_PER_CALL = 50
# 1チャンネルあたりRSSから拾う本数の上限。RSSはもともと15件前後しか
# 返さないので、実質は保険。
MAX_VIDEOS_PER_CHANNEL = 15
HTTP_TIMEOUT_SECONDS = 20


def log(message):
    """進捗は stderr へ。stdout は使わないが、混ざらないようにしておく。"""
    print(message, file=sys.stderr)


def fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": "CompanionEngine-feed/1.0"})
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
        return response.read()


def recent_video_ids(channel_id):
    """チャンネルのRSSから最近の動画IDを返す。取得できなければ None。

    空リストと None を区別しているのが重要。空リストは「本当に何もない」、
    None は「取れなかった」で、後者は前回の内容を引き継ぐ必要がある。
    """
    try:
        body = fetch(RSS_URL.format(channel_id=urllib.parse.quote(channel_id)))
    except (urllib.error.URLError, OSError) as error:
        log(f"  RSS failed for {channel_id}: {error}")
        return None
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError as error:
        log(f"  RSS unparseable for {channel_id}: {error}")
        return None

    ids = []
    for element in root.iter(VIDEO_ID_TAG):
        if element.text:
            ids.append(element.text.strip())
        if len(ids) >= MAX_VIDEOS_PER_CHANNEL:
            break
    return ids


def video_details(api_key, video_ids):
    """videos.list で動画のライブ状態を引く。{video_id: item} を返す。

    1件でも失敗したら例外を投げる。ライブ状態が分からないまま
    「誰も配信していない」フィードを書き出すと、クライアント側では
    配信が終わったように見えてしまうため、部分的な成功で先へ進めない。
    """
    details = {}
    for start in range(0, len(video_ids), MAX_IDS_PER_CALL):
        chunk = video_ids[start : start + MAX_IDS_PER_CALL]
        query = urllib.parse.urlencode(
            {
                "part": "snippet,liveStreamingDetails",
                "id": ",".join(chunk),
                "key": api_key,
                "maxResults": MAX_IDS_PER_CALL,
            }
        )
        body = fetch(f"{VIDEOS_API_URL}?{query}")
        payload = json.loads(body)
        for item in payload.get("items", []):
            details[item["id"]] = item
        log(f"  videos.list: {len(chunk)} ids -> {len(payload.get('items', []))} items (1 unit)")
    return details


def classify(channel_id, name, video_ids, details):
    """1チャンネルぶんのフィード項目を組み立てる。

    live は高々1件。YouTubeの仕様上ひとつのチャンネルが同時に複数の配信を
    持つことはあり得るが、デスクトップマスコットが伝えるのは1件で足りる。
    """
    entry = {"channel_id": channel_id, "name": name}
    upcoming = []

    for video_id in video_ids:
        item = details.get(video_id)
        if not item:
            continue
        snippet = item.get("snippet", {})
        streaming = item.get("liveStreamingDetails", {})
        state = snippet.get("liveBroadcastContent", "none")
        title = snippet.get("title", "")

        # actualEndTime があるものは終了済み。liveBroadcastContent が
        # 更新前でも、こちらを見れば取り違えない。
        if streaming.get("actualEndTime"):
            continue

        if state == "live" and "live" not in entry:
            entry["live"] = {
                "video_id": video_id,
                "title": title,
                "started_at": streaming.get("actualStartTime", ""),
            }
        elif state == "upcoming":
            scheduled_at = streaming.get("scheduledStartTime", "")
            if scheduled_at:
                upcoming.append(
                    {"video_id": video_id, "title": title, "scheduled_at": scheduled_at}
                )

    # 開始が早い順。クライアントは最も近い予定だけを表示に使う。
    upcoming.sort(key=lambda slot: slot["scheduled_at"])
    if upcoming:
        entry["upcoming"] = upcoming
    return entry


def load_previous(path):
    """前回のフィードの channels をそのままの順序で返す。無ければ空リスト。"""
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError) as error:
        log(f"previous feed unreadable ({error}); starting fresh")
        return []
    return [
        entry
        for entry in payload.get("channels", [])
        if isinstance(entry, dict) and "channel_id" in entry
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channels", required=True, help="監視するチャンネルの定義(JSON)")
    parser.add_argument("--out", required=True, help="書き出す streams.json のパス")
    args = parser.parse_args()

    api_key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    if not api_key:
        log("error: YOUTUBE_API_KEY is not set")
        return 1

    with open(args.channels, encoding="utf-8") as handle:
        channels = json.load(handle)
    if not isinstance(channels, list) or not channels:
        log(f"error: {args.channels} must be a non-empty JSON array")
        return 1

    previous_channels = load_previous(args.out)
    previous = {entry["channel_id"]: entry for entry in previous_channels}

    # まずRSSで候補のIDを集める(ここは無料)。
    per_channel_ids = {}
    all_ids = []
    for channel in channels:
        channel_id = channel["id"]
        log(f"RSS {channel_id} ({channel.get('name', '')})")
        ids = recent_video_ids(channel_id)
        per_channel_ids[channel_id] = ids
        if ids:
            all_ids.extend(ids)

    # 重複を除きつつ順序は保つ(同じ動画が複数チャンネルのRSSに出ることは
    # 通常ないが、コラボの再投稿などで起こりうる)。
    unique_ids = list(dict.fromkeys(all_ids))

    details = {}
    if unique_ids:
        try:
            details = video_details(api_key, unique_ids)
        except (urllib.error.URLError, OSError, ValueError, KeyError) as error:
            # ここで諦めるのが正しい。ライブ状態が引けないまま書き出すと、
            # 全員の配信が終わったかのようなフィードを配ってしまう。
            log(f"error: videos.list failed ({error}); leaving the previous feed in place")
            return 1

    entries = []
    for channel in channels:
        channel_id = channel["id"]
        name = channel.get("name", channel_id)
        ids = per_channel_ids.get(channel_id)
        if ids is None:
            # このチャンネルだけRSSが取れなかった。前回の内容を引き継ぐ。
            # 空で出すと、クライアント側では配信が終わったように見え、
            # 復旧時に同じ配信をもう一度通知してしまう。
            carried = previous.get(channel_id)
            if carried:
                log(f"  carrying over previous entry for {channel_id}")
                entries.append(carried)
            else:
                entries.append({"channel_id": channel_id, "name": name})
            continue
        entries.append(classify(channel_id, name, ids, details))

    # 内容が変わっていなければファイルに触らない。定期ジョブは10分おきに
    # 走るので、毎回書き換えるとリポジトリが無意味なコミットで膨らむ。
    # そのため generated_at は「最後に**内容が変わった**時刻」であって、
    # 「最後に確認した時刻」ではない。
    # 並びも含めて丸ごと比べる。channels.json からチャンネルを外したときに
    # 古い項目が残り続けないよう、辞書ではなくリストのまま突き合わせる。
    if previous_channels == entries:
        log("no change; leaving streams.json untouched")
        return 0

    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "channels": entries,
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8", newline="\n") as handle:
        # ensure_ascii=False で日本語をそのままUTF-8で書く。クライアントの
        # パーサは \uXXXX も解釈できるが、読める形のほうが調査が楽なので。
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")

    live_count = sum(1 for entry in entries if "live" in entry)
    log(f"wrote {args.out}: {len(entries)} channels, {live_count} live")
    return 0


if __name__ == "__main__":
    sys.exit(main())
