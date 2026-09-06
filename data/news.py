"""ニュース候補の機械的な収集（STEP7）。

パイプライン（すべて機械的処理。AIによる解釈は一切行わない。STEP7ではAI APIを一切使用しない）:
    RSS取得 → 直近24時間フィルタ → 重複除去（URL完全一致→正規化URL→タイトル類似度）
    → テーマ分類（ルールベース） → データ構造化

- 本文は取得・保存しない。title/url/published_at/source/category のみ保持する
  （加えて追跡用に matched_keyword・feed を付与している）。
- 取得できなかった情報を推測で補完しない。RSS取得に失敗したキーワードがあっても、
  他のキーワードの結果はそのまま返し、失敗内容は errors に積む。
- STEP7ではHTML表示は行わない（templates/morning11.html・generator/html.py は変更しない）。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import requests

GOOGLE_NEWS_RSS_URL = "https://news.google.com/rss/search"
JST = ZoneInfo("Asia/Tokyo")
REQUEST_TIMEOUT = 15
USER_AGENT = "Mozilla/5.0 (compatible; morning11-bot/1.0)"

_WS_RE = re.compile(r"\s+")
_NON_ALNUM_RE = re.compile(r"[^A-Za-z0-9぀-ヿ一-鿿]+")

# テーマ分類（ルールベース）: 検索キーワード → 正規化されたカテゴリ
# STEP8以降でAIが意味解釈を加える前提の、機械的な一次分類。
CATEGORY_MAP: dict[str, str] = {
    "NVIDIA": "nvidia",
    "AI": "ai",
    "半導体": "semiconductor",
    "日銀": "boj",
    "為替": "fx",
    "金利": "rates",
    "米金融政策": "us_market",
    "日本株": "japan_market",
    "中国": "china",
    "原油・エネルギー": "energy",
    "防衛": "defense",
}
VALID_CATEGORIES = set(CATEGORY_MAP.values()) | {"other"}


@dataclass
class NewsItem:
    title: str
    url: str
    published_at: str | None  # JST ISO8601（例 "2026-09-06T21:40:00+09:00"）。不明な場合はNone
    source: str
    category: str
    matched_keyword: str
    feed: str = "google_news_rss"

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "url": self.url,
            "published_at": self.published_at,
            "source": self.source,
            "category": self.category,
            "matched_keyword": self.matched_keyword,
            "feed": self.feed,
        }


def _strip_source_suffix(title: str, source: str | None) -> str:
    """Google Newsはタイトル末尾に ' - ソース名' を重複して付与するため取り除く。"""
    if source and title.endswith(f" - {source}"):
        return title[: -(len(source) + 3)].strip()
    return title


def _to_jst_iso(pub_date: str | None) -> str | None:
    """RFC822形式のpubDate文字列をJSTのISO8601文字列に変換する。パースできなければNone。"""
    if not pub_date:
        return None
    try:
        dt = parsedate_to_datetime(pub_date)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(JST).isoformat()


def _normalize_url(url: str) -> str:
    """クエリ文字列・フラグメントを除いた正規化URL(重複判定の第2段階に使用)。"""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def _normalize_title(title: str) -> str:
    return _WS_RE.sub(" ", title).strip().casefold()


def _title_tokens(title: str) -> set[str]:
    """類似度判定用の特徴量。日本語は分かち書きされないため、記号を除いた文字列上の
    文字bigram（2文字の滑り窓）を使う。同じ見出しの一部を含む/含まれる関係
    （例: 「半導体メモリー急伸」と「半導体株が上昇 半導体メモリー急伸で連想買い」）を
    単語分割なしに機械的に検出できる、簡易だが実用的な手法。
    """
    normalized = _NON_ALNUM_RE.sub("", title.casefold())
    if len(normalized) < 2:
        return {normalized} if normalized else set()
    return {normalized[i : i + 2] for i in range(len(normalized) - 1)}


def _title_similarity(a: set[str], b: set[str]) -> float:
    """重なり係数(Overlap coefficient) = |A∩B| / min(|A|,|B|)。

    一方の見出しがもう一方を部分文字列として含むケース（同じニュースを別の媒体が
    前置き・接尾辞付きで報じるケース）でも高いスコアになるよう、Jaccardではなく
    重なり係数を採用している。
    """
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def _fetch_rss_text(keyword: str, lang: str, country: str) -> str:
    params = {"q": keyword, "hl": lang, "gl": country, "ceid": f"{country}:{lang}"}
    resp = requests.get(
        GOOGLE_NEWS_RSS_URL, params=params, timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT}
    )
    resp.raise_for_status()
    return resp.text


def _parse_rss_items(xml_text: str) -> list[dict[str, str | None]]:
    root = ET.fromstring(xml_text)
    channel = root.find("channel")
    if channel is None:
        return []
    items = []
    for item_el in channel.findall("item"):
        title_el = item_el.find("title")
        link_el = item_el.find("link")
        pubdate_el = item_el.find("pubDate")
        source_el = item_el.find("source")
        source = source_el.text.strip() if source_el is not None and source_el.text else "(不明なソース)"
        raw_title = title_el.text.strip() if title_el is not None and title_el.text else ""
        items.append(
            {
                "title": _strip_source_suffix(raw_title, source),
                "url": link_el.text.strip() if link_el is not None and link_el.text else "",
                "pub_date": pubdate_el.text.strip() if pubdate_el is not None and pubdate_el.text else None,
                "source": source,
            }
        )
    return items


def fetch_keyword_news(
    keyword: str, max_items: int, lang: str = "ja", country: str = "JP"
) -> tuple[list[dict[str, str | None]], str | None]:
    """1キーワード分のニュース候補を取得する。戻り値は (items, error)。"""
    try:
        xml_text = _fetch_rss_text(keyword, lang, country)
        items = _parse_rss_items(xml_text)
    except Exception as exc:  # noqa: BLE001
        return [], f"google_news_rss('{keyword}'): {exc}"
    return items[:max_items], None


def categorize(keyword: str) -> str:
    """ルールベースのテーマ分類。未知のキーワードは 'other' にフォールバックする。"""
    return CATEGORY_MAP.get(keyword, "other")


def filter_recent(items: list[NewsItem], now: datetime, hours: int = 24) -> list[NewsItem]:
    """公開日時(published_at)が now から遡って hours 時間以内のものだけを残す。

    取得日時(now/fetched_at)と公開日時(published_at)を混同しないよう、
    フィルタの基準は必ず published_at 側とする。published_at が不明な項目は
    24時間以内かどうか判定できないため、推測せず除外する。
    """
    cutoff = now - timedelta(hours=hours)
    recent: list[NewsItem] = []
    for item in items:
        if not item.published_at:
            continue
        try:
            published = datetime.fromisoformat(item.published_at)
        except ValueError:
            continue
        if cutoff <= published <= now:
            recent.append(item)
    return recent


def dedupe(items: list[NewsItem], similarity_threshold: float = 0.75) -> list[NewsItem]:
    """URL完全一致→正規化URL→タイトル類似度の順で重複を除去する（先勝ち）。

    タイトル類似度は異なる記事の過剰な統合を避けるため、閾値をやや高めに設定している。
    """
    kept: list[NewsItem] = []
    seen_urls: set[str] = set()
    seen_normalized_urls: set[str] = set()
    kept_tokens: list[set[str]] = []

    for item in items:
        if item.url in seen_urls:
            continue
        normalized_url = _normalize_url(item.url)
        if normalized_url in seen_normalized_urls:
            continue

        tokens = _title_tokens(item.title)
        if any(_title_similarity(tokens, existing) >= similarity_threshold for existing in kept_tokens):
            continue

        kept.append(item)
        seen_urls.add(item.url)
        seen_normalized_urls.add(normalized_url)
        kept_tokens.append(tokens)

    return kept


def select_for_display(
    news_items: list[NewsItem],
    category_priority: list[str],
    max_per_category: int = 3,
    max_total: int = 10,
) -> list[NewsItem]:
    """STEP8-A: 「注目ニュース」欄に載せる項目を機械的に選定する（表示選定のみ、データは削除しない）。

    - category_priority に列挙された順にカテゴリを処理し、各カテゴリ内は published_at の
      新しい順に最大 max_per_category 件を採用する。
    - 全体の採用件数が max_total に達した時点で打ち切る（優先度の低いカテゴリほど
      採用件数が減る、または0件になりうる）。
    - category_priority に含まれないカテゴリ（例: us_market, other）は、この選定結果には
      含めない。ただし呼び出し元が渡す news_items 自体は変更しないため、他の用途
      （将来のAI分析等）では引き続き全カテゴリを参照できる。
    - タイトルの内容から重要度を推測するようなことはしない。あくまで
      「カテゴリ優先順位」と「新しさ」だけの機械的な選定。
    """
    by_category: dict[str, list[NewsItem]] = {}
    for item in news_items:
        by_category.setdefault(item.category, []).append(item)
    for items in by_category.values():
        items.sort(key=lambda n: n.published_at or "", reverse=True)

    selected: list[NewsItem] = []
    remaining = max_total
    for category in category_priority:
        if remaining <= 0:
            break
        take = by_category.get(category, [])[: min(max_per_category, remaining)]
        selected.extend(take)
        remaining -= len(take)

    return selected


def fetch_news(settings: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """設定のキーワードごとにRSSを取得し、24時間フィルタ・重複除去・分類まで行う。

    戻り値は {"news": [NewsItem...], "fetched_at": ISO文字列, "errors": [...]}。
    fetched_at は取得処理を実行した時刻であり、各記事の published_at とは別物。
    """
    now = now or datetime.now(JST)
    news_settings = settings.get("news", {})
    keywords: list[str] = news_settings.get("keywords", [])
    max_items_per_keyword = news_settings.get("max_items_per_keyword", 10)
    max_total_items = news_settings.get("max_total_items", 30)
    recent_hours = news_settings.get("recent_hours", 24)
    similarity_threshold = news_settings.get("dedup_similarity_threshold", 0.75)

    candidates: list[NewsItem] = []
    errors: list[str] = []

    for keyword in keywords:
        raw_items, error = fetch_keyword_news(keyword, max_items_per_keyword)
        if error:
            errors.append(error)
        for raw in raw_items:
            if not raw["title"] or not raw["url"]:
                continue
            candidates.append(
                NewsItem(
                    title=raw["title"],
                    url=raw["url"],
                    published_at=_to_jst_iso(raw.get("pub_date")),
                    source=raw["source"],
                    category=categorize(keyword),
                    matched_keyword=keyword,
                )
            )

    recent = filter_recent(candidates, now, hours=recent_hours)
    deduped = dedupe(recent, similarity_threshold)
    deduped.sort(key=lambda n: n.published_at or "", reverse=True)

    return {
        "news": deduped[:max_total_items],
        "fetched_at": now.isoformat(),
        "errors": errors,
    }


if __name__ == "__main__":
    from data.market import load_settings

    out = fetch_news(load_settings())
    print(f"fetched_at: {out['fetched_at']}  count: {len(out['news'])}")
    for n in out["news"]:
        print(n.to_dict())
    if out["errors"]:
        print("errors:", out["errors"])
