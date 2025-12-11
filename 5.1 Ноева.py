import requests
from bs4 import BeautifulSoup
import sqlite3
import uuid
from datetime import datetime, timezone
import re
import time
import concurrent.futures
from threading import Lock
import logging
import random
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Глобальные переменные для статистики
processed_urls = set()
db_lock = Lock()

# Ротация User-Agent
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.1.1 Safari/605.1.15',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/92.0.4515.107 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:90.0) Gecko/20100101 Firefox/90.0',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:90.0) Gecko/20100101 Firefox/90.0'
]


def get_random_headers():
    return {
        'User-Agent': random.choice(USER_AGENTS),
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'ru-RU,ru;q=0.8,en-US;q=0.5,en;q=0.3',
        'Accept-Encoding': 'gzip, deflate',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
    }


def create_session_with_retries():
    session = requests.Session()

    retry_strategy = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
    )

    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    return session


class RateLimiter:
    def __init__(self, calls_per_second=2):
        self.calls_per_second = calls_per_second
        self.last_call_time = 0
        self.lock = Lock()

    def wait(self):
        with self.lock:
            current_time = time.time()
            time_since_last_call = current_time - self.last_call_time
            min_interval = 1.0 / self.calls_per_second

            if time_since_last_call < min_interval:
                time.sleep(min_interval - time_since_last_call)

            self.last_call_time = time.time()


# Глобальный rate limiter
rate_limiter = RateLimiter(calls_per_second=2)


def clean_html_content(soup):
    # Удаляем скрипты, стили, медиа-элементы
    for element in soup(['script', 'style', 'iframe', 'video', 'audio', 'object', 'embed']):
        element.decompose()

    # Удаляем изображения и галереи
    for img in soup.find_all('img'):
        img.decompose()

    # Удаляем пустые элементы
    for element in soup.find_all():
        if len(element.get_text(strip=True)) == 0:
            element.decompose()

    # Получаем чистый текст
    text = soup.get_text(separator='\n', strip=True)

    # Очищаем множественные переносы строк
    text = re.sub(r'\n\s*\n', '\n\n', text)
    text = re.sub(r'[ \t]+', ' ', text)

    return text.strip()


def parse_published_date(date_text, url):
    """Парсит дату публикации и приводит к стандартному формату"""
    try:
        if not date_text:
            return datetime.now(timezone.utc).isoformat()

        return datetime.now(timezone.utc).isoformat()

    except Exception as e:
        logger.debug(f"Ошибка парсинга даты '{date_text}' для {url}: {e}")
        return datetime.now(timezone.utc).isoformat()


def parse_full_article_fast(article_url):
    """Быстрый парсинг текста статьи"""
    try:
        session = create_session_with_retries()
        response = session.get(article_url, headers=get_random_headers(), timeout=10)
        response.raise_for_status()

        soup = BeautifulSoup(response.content, 'lxml')

        # Быстрый поиск контента
        content_element = soup.find('div', class_='content')
        if not content_element:
            return "Текст не найден"

        # Используем улучшенную очистку
        cleaned_text = clean_html_content(content_element)

        # Проверяем, что текст достаточно содержательный
        if len(cleaned_text.split()) < 10:
            return "Слишком короткий текст"

        return cleaned_text[:4000] if len(cleaned_text) > 4000 else cleaned_text

    except Exception as e:
        logger.debug(f"Ошибка статьи {article_url}: {e}")
        return "Ошибка загрузки"


def parse_page_links(page_num, base_url):
    """Парсинг ссылок с одной страницы"""
    try:
        # Ограничиваем скорость запросов
        rate_limiter.wait()

        if page_num == 1:
            url = f'{base_url}/news'
        else:
            url = f'{base_url}/news?page={page_num}'

        session = create_session_with_retries()
        response = session.get(url, headers=get_random_headers(), timeout=10)

        if response.status_code == 404:
            return None  # Конец страниц

        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'lxml')

        news_data = []
        news_blocks = soup.find_all('div', class_='news news_bordered')

        for block in news_blocks:
            try:
                title_element = block.find('a', class_='news__title')
                if not title_element:
                    continue

                title = title_element.get_text(strip=True)
                url_path = title_element.get('href', '')

                if not title or not url_path:
                    continue

                full_url = base_url + url_path if url_path.startswith('/') else url_path

                if full_url in processed_urls:
                    continue

                processed_urls.add(full_url)

                date_element = block.find('div', class_='mr-auto')
                date_text = date_element.get_text(strip=True) if date_element else ""

                comments_element = block.find('a', class_='news__comments mr-15')
                comments_count = 0
                if comments_element:
                    comments_text = comments_element.get_text(strip=True)
                    numbers = re.findall(r'\d+', comments_text)
                    if numbers:
                        comments_count = int(numbers[0])

                news_data.append({
                    'guid': str(uuid.uuid4()),
                    'title': title,
                    'description': '',
                    'url': full_url,
                    'published_at': parse_published_date(date_text, full_url),
                    'comments_count': comments_count,
                    'created_at_utc': datetime.now(timezone.utc).isoformat(),
                    'rating': None
                })

            except Exception as e:
                logger.debug(f"Ошибка блока на странице {page_num}: {e}")
                continue

        return news_data

    except Exception as e:
        logger.warning(f"Ошибка страницы {page_num}: {e}")
        return []


def collect_all_links_parallel(base_url, max_pages=300, max_workers=10):
    """Параллельный сбор всех ссылок"""
    logger.info(f"Начало сбора ссылок с {max_pages} страниц...")

    all_news_data = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        # Создаем задачи для всех страниц
        future_to_page = {executor.submit(parse_page_links, page_num, base_url): page_num
                          for page_num in range(1, max_pages + 1)}

        completed = 0
        for future in concurrent.futures.as_completed(future_to_page):
            page_num = future_to_page[future]
            try:
                page_data = future.result()
                if page_data is None:  # Конец страниц
                    logger.info(f"Достигнут конец на странице {page_num}")
                    break

                all_news_data.extend(page_data)
                completed += 1

                if completed % 10 == 0:
                    logger.info(f"Обработано страниц: {completed}, собрано новостей: {len(all_news_data)}")

            except Exception as e:
                logger.warning(f"Ошибка выполнения страницы {page_num}: {e}")

    logger.info(f"Сбор ссылок завершен. Всего новостей: {len(all_news_data)}")
    return all_news_data


def parse_articles_parallel(news_data, max_workers=15):
    """Параллельный парсинг текстов статей"""
    logger.info(f"Начало парсинга текстов {len(news_data)} статей...")

    def process_article(news_item):
        try:
            content = parse_full_article_fast(news_item['url'])
            news_item['description'] = content
            return news_item
        except Exception as e:
            logger.debug(f"Ошибка обработки статьи: {e}")
            news_item['description'] = "Ошибка загрузки"
            return news_item

    # Разбиваем на батчи для лучшего контроля
    batch_size = 50
    processed_data = []

    for i in range(0, len(news_data), batch_size):
        batch = news_data[i:i + batch_size]
        logger.info(f"Обработка батча {i // batch_size + 1}/{(len(news_data) - 1) // batch_size + 1}")

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            batch_results = list(executor.map(process_article, batch))
            processed_data.extend(batch_results)

        # Сохраняем прогресс после каждого батча
        save_progress(processed_data)

        # Небольшая пауза между батчами
        if i + batch_size < len(news_data):
            time.sleep(1)

    logger.info(f"Парсинг текстов завершен. Обработано: {len(processed_data)} статей")
    return processed_data


def save_progress(data, db_name='news.db'):
    """Сохранение прогресса в базу данных"""

    with db_lock:
        conn = sqlite3.connect(db_name)
        cursor = conn.cursor()

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS news_articles (
                guid TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT,
                url TEXT NOT NULL UNIQUE,
                published_at TEXT,
                comments_count INTEGER,
                created_at_utc TEXT NOT NULL,
                rating INTEGER
            )
        ''')

        # Создаем индекс для ускорения поиска
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_url ON news_articles(url)')
        if data:
            for news in data:
                try:
                    cursor.execute('''
                        INSERT OR IGNORE INTO news_articles 
                        (guid, title, description, url, published_at, comments_count, created_at_utc, rating)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        news['guid'],
                        news['title'],
                        news['description'],
                        news['url'],
                        news['published_at'],
                        news['comments_count'],
                        news['created_at_utc'],
                        news['rating']
                    ))
                except Exception as e:
                    logger.debug(f"Ошибка сохранения: {e}")

        conn.commit()
        conn.close()


def print_statistics(db_name='news.db'):
    """Выводит статистику по собранным данным"""
    conn = sqlite3.connect(db_name)
    cursor = conn.cursor()

    cursor.execute('SELECT COUNT(*) FROM news_articles')
    total_count = cursor.fetchone()[0]

    cursor.execute('SELECT COUNT(DISTINCT url) FROM news_articles')
    unique_urls = cursor.fetchone()[0]

    cursor.execute(
        'SELECT COUNT(*) FROM news_articles WHERE description IS NULL OR description = "" OR description = "Текст не найден" OR description = "Ошибка загрузки" OR description = "Слишком короткий текст"')
    empty_descriptions = cursor.fetchone()[0]

    cursor.execute(
        'SELECT COUNT(*) FROM news_articles WHERE description NOT IN ("Текст не найден", "Ошибка загрузки", "Слишком короткий текст") AND description IS NOT NULL AND description != ""')
    valid_articles = cursor.fetchone()[0]

    print(f"\nСТАТИСТИКА")
    print(f"Всего записей: {total_count}")
    print(f"Уникальных URL: {unique_urls}")
    print(f"Статей с валидным текстом: {valid_articles}")
    print(f"Статей без текста/с ошибками: {empty_descriptions}")

    conn.close()


def fast_parse_complete(max_pages=300, db_name='news.db'):
    """Полный ускоренный парсинг"""
    base_url = 'https://www.amur.life'

    # Очищаем глобальные переменные для нового парсинга
    global processed_urls
    processed_urls = set()

    # Создаем базу заранее
    save_progress([], db_name)

    # Быстрый сбор всех ссылок
    logger.info("Сбор ссылок ")
    start_time = time.time()

    all_news_data = collect_all_links_parallel(base_url, max_pages, max_workers=15)

    link_time = time.time()
    logger.info(f"Сбор ссылок занял: {link_time - start_time:.2f} секунд")

    if not all_news_data:
        logger.error("Не удалось собрать ссылки")
        return []

    #  Парсинг текстов статей
    final_data = parse_articles_parallel(all_news_data, max_workers=20)

    end_time = time.time()
    logger.info(f"Парсинг текстов занял: {end_time - link_time:.2f} секунд")
    logger.info(f"Общее время: {end_time - start_time:.2f} секунд")
    logger.info(f"Итого обработано: {len(final_data)} статей")

    save_progress(final_data, db_name)

    print_statistics(db_name)

    return final_data


if __name__ == "__main__":

    final_data = fast_parse_complete(max_pages=300, db_name='news.db')

    if final_data:
        print(f"\nПарсинг завершен! Обработано {len(final_data)} статей")
    else:
        print("Парсинг не удался")