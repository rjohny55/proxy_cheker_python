#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import json
import time
import threading
import subprocess
import re
import platform
import queue
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

# --- Попытка импорта зависимостей ---
try:
    import requests
    import urllib3
    from colorama import init, Fore, Style
    # Отключаем надоедливые предупреждения о небезопасном SSL (так как verify=False)
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except ImportError:
    print("Ошибка: Не найдены библиотеки requests или colorama.")
    print("pip install requests colorama")
    sys.exit(1)

# --- Константы ---
CONFIG_FILE = Path("config.json")
DEFAULT_CONFIG = {
    "threads": 100,             # Количество потоков
    "timeout": 10,              # Таймаут соединения (сек)
    "max_ms": 3000,             # Макс. задержка (мс) для сохранения
    "import_files": ["proxies.txt"],
    "export_file": "good_proxies.txt",
    "host_check_url": "https://www.google.com",
    "enable_ping": True,        # Включить системный пинг
    "ping_timeout_ms": 1000,
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

# --- Глобальные переменные ---
stats = {
    "checked": 0,
    "good": 0,
    "total": 0
}
stats_lock = threading.Lock()
ping_command_available = False # Флаг доступности команды ping
result_queue = queue.Queue()   # Очередь для записи в файл

# --- Функции конфигурации ---

def load_or_create_config():
    """Загружает конфиг или создает дефолтный, если нет файла."""
    if not CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(DEFAULT_CONFIG, f, indent=4)
            print(f"{Fore.YELLOW}Создан файл {CONFIG_FILE}. Пожалуйста, настройте его при необходимости.{Style.RESET_ALL}")
        except Exception as e:
            print(f"{Fore.RED}Ошибка создания конфига: {e}{Style.RESET_ALL}")
            return DEFAULT_CONFIG

    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            loaded = json.load(f)
            # Merge с дефолтным, чтобы не ломалось при обновлении версий
            config = DEFAULT_CONFIG.copy()
            config.update(loaded)
            return config
    except Exception as e:
        print(f"{Fore.RED}Ошибка чтения конфига: {e}. Используются настройки по умолчанию.{Style.RESET_ALL}")
        return DEFAULT_CONFIG

def check_ping_availability():
    """Проверяет, доступна ли команда ping в системе."""
    param = '-n' if platform.system().lower() == 'windows' else '-c'
    cmd = ['ping', param, '1', '127.0.0.1']
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1)
        return True
    except (subprocess.SubprocessError, FileNotFoundError):
        return False

# --- Вспомогательные функции ---

def parse_proxy(proxy_str):
    """
    Преобразует строку прокси в формат для requests.
    Поддерживает: 
    - ip:port
    - ip:port:user:pass
    - user:pass@ip:port
    """
    proxy_str = proxy_str.strip()
    if not proxy_str: return None
    
    # Если уже есть протокол, убираем его для парсинга
    if '://' in proxy_str:
        proxy_str = proxy_str.split('://')[-1]

    parts = proxy_str.split(':')
    
    # Формат ip:port
    if len(parts) == 2:
        return {
            "http": f"http://{proxy_str}",
            "https": f"http://{proxy_str}",
            "raw": proxy_str,
            "ip": parts[0]
        }
    
    # Формат ip:port:user:pass
    elif len(parts) == 4:
        ip, port, user, password = parts
        formatted = f"{user}:{password}@{ip}:{port}"
        return {
            "http": f"http://{formatted}",
            "https": f"http://{formatted}",
            "raw": proxy_str, # Сохраняем как было в оригинале или можно formatted
            "ip": ip
        }
    
    # Формат user:pass@ip:port
    elif '@' in proxy_str:
        try:
            auth, host = proxy_str.split('@')
            ip = host.split(':')[0]
            return {
                "http": f"http://{proxy_str}",
                "https": f"http://{proxy_str}",
                "raw": proxy_str,
                "ip": ip
            }
        except:
            pass

    return None

def system_ping(ip, timeout_ms):
    """Выполняет ICMP пинг."""
    if not ping_command_available:
        return None
        
    try:
        timeout_sec = timeout_ms / 1000.0
        # Windows
        if platform.system().lower() == 'windows':
            cmd = ['ping', '-n', '1', '-w', str(timeout_ms), ip]
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            kwargs = {'startupinfo': startupinfo, 'creationflags': subprocess.CREATE_NO_WINDOW}
        # Linux / Mac
        else:
            cmd = ['ping', '-c', '1', '-W', str(timeout_sec), ip]
            kwargs = {}

        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec + 0.2, **kwargs)
        
        if res.returncode == 0:
            # Ищем время
            match = re.search(r"time[=<]([\d.]+)\s?ms", res.stdout, re.IGNORECASE)
            if match:
                return int(float(match.group(1)))
            return 1 # Успех, но время не распарсилось (<1ms)
    except:
        pass
    return None

# --- Поток записи в файл (Consumer) ---

def file_writer_worker(filepath):
    """Отдельный поток, который забирает результаты из очереди и пишет в файл."""
    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            while True:
                item = result_queue.get()
                if item is None: # Сигнал остановки
                    break
                f.write(item + '\n')
                f.flush() # Принудительная запись на диск
                result_queue.task_done()
    except IOError as e:
        print(f"{Fore.RED}Критическая ошибка записи файла: {e}{Style.RESET_ALL}")

# --- Основная логика проверки ---

def check_single_proxy(proxy_raw, config):
    proxy_data = parse_proxy(proxy_raw)
    if not proxy_data:
        return

    proxy_dict = {"http": proxy_data['http'], "https": proxy_data['https']}
    headers = {'User-Agent': config['user_agent']}
    
    start_time = time.perf_counter()
    latency = None
    ping_time = None
    is_alive = False
    
    try:
        # ИСПРАВЛЕНИЕ: Используем GET с stream=True вместо HEAD
        # verify=False нужен для многих HTTPS прокси с самоподписанными сертификатами
        with requests.get(
            config['host_check_url'], 
            proxies=proxy_dict, 
            timeout=config['timeout'], 
            stream=True, 
            verify=False,
            headers=headers
        ) as r:
            # Читаем статус, но не качаем тело
            if 200 <= r.status_code < 400:
                is_alive = True
            # Соединение закрывается автоматически при выходе из with

        end_time = time.perf_counter()
        latency = int((end_time - start_time) * 1000)

        # Если HTTP успешен, пробуем пинг (если включен глобально и в конфиге)
        if is_alive and config['enable_ping'] and ping_command_available:
            ping_time = system_ping(proxy_data['ip'], config['ping_timeout_ms'])

    except (requests.exceptions.ProxyError, requests.exceptions.ConnectTimeout, requests.exceptions.ReadTimeout, requests.exceptions.SSLError):
        pass # Обычные ошибки прокси
    except Exception:
        pass # Остальные ошибки

    # --- Обработка результата ---
    status_msg = ""
    status_color = Fore.RED

    if is_alive:
        if latency <= config['max_ms']:
            status_color = Fore.GREEN
            status_msg = f"HTTP: {latency}ms"
            if ping_time is not None:
                status_msg += f" | Ping: {ping_time}ms"
            
            # Добавляем в очередь на запись (сохраняем оригинальную строку)
            result_queue.put(proxy_data['raw'])
            
            with stats_lock:
                stats['good'] += 1
        else:
            status_color = Fore.YELLOW
            status_msg = f"Slow: {latency}ms"
    else:
        status_msg = "Dead / Timeout"

    # Вывод
    # Ограничиваем длину прокси для красивой таблицы
    display_proxy = (proxy_data['raw'][:25] + '..') if len(proxy_data['raw']) > 25 else proxy_data['raw']
    print(f"{Fore.WHITE}{display_proxy:<30} {status_color}{status_msg}{Style.RESET_ALL}")

    # Обновление статистики и заголовка
    with stats_lock:
        stats['checked'] += 1
        current_title = f"Proxy Checker | {stats['checked']}/{stats['total']} | Good: {stats['good']}"
    
    if platform.system() == 'Windows':
        import ctypes
        ctypes.windll.kernel32.SetConsoleTitleW(current_title)
    else:
        sys.stdout.write(f"\x1b]2;{current_title}\x07")
        sys.stdout.flush()

# --- Точка входа ---

def main():
    init() # Colorama
    os.system('cls' if os.name == 'nt' else 'clear')
    
    # 1. Загрузка конфига
    config = load_or_create_config()
    
    # 2. Проверка доступности пинга
    global ping_command_available
    ping_command_available = check_ping_availability()
    if config['enable_ping'] and not ping_command_available:
        print(f"{Fore.YELLOW}Внимание: Команда 'ping' недоступна. Пинг отключен.{Style.RESET_ALL}")
    
    # 3. Загрузка прокси
    print(f"{Fore.CYAN}Загрузка прокси из {config['import_files']}...{Style.RESET_ALL}")
    proxies = set()
    for filename in config['import_files']:
        path = Path(filename)
        if path.exists():
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    for line in f:
                        clean = line.strip()
                        if clean and ':' in clean: # Базовая валидация
                            proxies.add(clean)
            except Exception as e:
                print(f"{Fore.RED}Ошибка чтения {filename}: {e}{Style.RESET_ALL}")
        else:
            print(f"{Fore.YELLOW}Файл {filename} не найден{Style.RESET_ALL}")
    
    proxies_list = list(proxies)
    stats['total'] = len(proxies_list)
    
    if stats['total'] == 0:
        print(f"{Fore.RED}Нет прокси для проверки. Выход.{Style.RESET_ALL}")
        return

    print(f"Загружено уникальных прокси: {stats['total']}")
    print(f"Потоков: {config['threads']} | Таймаут: {config['timeout']}s")
    print("-" * 50)

    # 4. Запуск потока записи
    writer_thread = threading.Thread(target=file_writer_worker, args=(config['export_file'],), daemon=True)
    writer_thread.start()

    # 5. Запуск пула потоков
    try:
        with ThreadPoolExecutor(max_workers=config['threads']) as executor:
            # Отправляем задачи
            futures = [executor.submit(check_single_proxy, p, config) for p in proxies_list]
            
            # Ждем завершения (можно было бы as_completed, но нам не обязательно)
            for future in futures:
                future.result() # Чтобы ловить исключения если они всплывут
                
    except KeyboardInterrupt:
        print(f"\n{Fore.YELLOW}Остановка пользователем...{Style.RESET_ALL}")
    
    # 6. Завершение работы с очередью
    result_queue.put(None) # Сигнал писателю остановиться
    writer_thread.join()   # Ждем пока допишет остатки

    print("\n" + "=" * 50)
    print(f"{Fore.GREEN}Проверка завершена.{Style.RESET_ALL}")
    print(f"Всего проверено: {stats['checked']}")
    print(f"Рабочих (сохранено): {stats['good']}")
    print(f"Результат в файле: {config['export_file']}")
    
    input("Нажмите Enter, чтобы выйти...")

if __name__ == "__main__":
    main()
