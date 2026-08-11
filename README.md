# WeatherWebsite — Balloon Trajectory Predictor

Веб-приложение для расчёта траектории полёта метеозонда на основе данных NOAA GFS из AWS Open Data. Построено на Django + Yandex Maps JS API.

![Python](https://img.shields.io/badge/Python-3.10+-blue?logo=python&logoColor=white)
![Django](https://img.shields.io/badge/Django-4.x-green?logo=django&logoColor=white)
![Yandex Maps](https://img.shields.io/badge/Yandex_Maps_JS_API-2.1-red)
![Queue](https://img.shields.io/badge/Queue-django--rq_+_Redis-critical)
![Data](https://img.shields.io/badge/Wind_Data-NOAA_GFS_on_AWS-blue)

---

## Возможности

- Интерактивная карта — кликните для выбора точки старта
- Два профиля полёта: **стандартный** (подъём → разрыв → спуск) и **парящий** (подъём → плавание → спуск)
- Реальные данные ветра NOAA GFS на всех высотах (1000–1 гПа в зависимости от сетки)
- Выбор сетки расчёта: приближенная 1.0° или точная 0.5°
- Цветная траектория по фазам: подъём / плавание / спуск
- Балуны с координатами: десятичные градусы, DMS и СК-42 (Гаусса-Крюгера), скорость и направление сноса
- Экспорт результата в Excel (CSV) и KML для Google Earth
- Расчёт идёт в фоновой очереди, браузер опрашивает статус и показывает прогресс
- Работает в России без VPN (Яндекс.Карты)

---

## Установка

```bash
git clone https://github.com/jbuptown/WeatherWebsite.git
cd WeatherWebsite

python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # Linux/macOS

pip install -r requirements.txt

python manage.py migrate
python manage.py runserver
```

Расчёт выполняется в фоновой очереди, поэтому нужен запущенный Redis
(адрес и порт задаются переменными `REDIS_HOST` / `REDIS_PORT`, по умолчанию
`127.0.0.1:6379`) и хотя бы один обработчик — в отдельном терминале:

```bash
python manage.py rqworker default
# Windows: python manage.py rqworker default --worker-class rq.SimpleWorker
```

Откройте http://127.0.0.1:8000

---

## Стек

| Слой | Технология |
|---|---|
| Backend | Django (Python) |
| Очередь | django-rq + Redis |
| Frontend | Yandex Maps JS API 2.1 |
| Карта | Яндекс.Карты |
| Данные ветра | NOAA GFS AWS Open Data |
| БД | SQLite |

---

## API

| Метод | URL | Описание |
|---|---|---|
| `POST` | `/api/predict/start/` | Поставить расчёт в очередь, вернуть `task_id` |
| `GET` | `/api/predict/status/<task_id>/` | Статус задачи, а по готовности — результат |
| `POST` | `/api/predict/` | Синхронный расчёт (без очереди) |
| `GET` | `/api/points/` | Список точек на карте |
| `POST` | `/api/points/add/` | Добавить точку |
| `DELETE` | `/api/points/<id>/delete/` | Удалить точку |

Интерфейс использует пару `start` + `status`: длинный расчёт не упирается в
таймаут прокси. Синхронный `/api/predict/` оставлен для скриптов.

### Параметры `/api/predict/start/` и `/api/predict/`

```json
{
  "latitude": 55.7558,
  "longitude": 37.6176,
  "altitude": 100,
  "launch_date": "2026-05-20",
  "launch_time": "06:00",
  "ascent_rate": 3.0,
  "float_altitude": 5000,
  "burst_altitude": 30000,
  "descent_rate": 5.0,
  "profile": "standard"
}
```

---

## Лицензия

MIT
