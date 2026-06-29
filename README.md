# WeatherWebsite — Balloon Trajectory Predictor

Веб-приложение для расчёта траектории полёта метеозонда на основе данных NOAA GFS из AWS Open Data. Построено на Django + MapLibre GL JS.

![Python](https://img.shields.io/badge/Python-3.10+-blue?logo=python&logoColor=white)
![Django](https://img.shields.io/badge/Django-4.x-green?logo=django&logoColor=white)
![MapLibre](https://img.shields.io/badge/MapLibre_GL_JS-4.5-orange)
![Data](https://img.shields.io/badge/Wind_Data-NOAA_GFS_on_AWS-blue)

---

## Возможности

- Интерактивная карта — кликните для выбора точки старта
- Два профиля полёта: **стандартный** (подъём → разрыв → спуск) и **парящий** (подъём → плавание → спуск)
- Реальные данные ветра NOAA GFS на всех высотах (1000–1 гПа в зависимости от сетки)
- Выбор сетки расчёта: приближенная 1.0° или точная 0.5°
- Цветная траектория по фазам: подъём / плавание / спуск
- Попапы с координатами старта и финиша
- Работает в России без VPN (OpenStreetMap + jsDelivr CDN)

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

Откройте http://127.0.0.1:8000

---

## Стек

| Слой | Технология |
|---|---|
| Backend | Django (Python) |
| Frontend | MapLibre GL JS 4.5 |
| Карта | OpenStreetMap |
| Данные ветра | NOAA GFS AWS Open Data |
| БД | SQLite |

---

## API

| Метод | URL | Описание |
|---|---|---|
| `POST` | `/api/predict/` | Рассчитать траекторию |
| `GET` | `/api/points/` | Список точек на карте |
| `POST` | `/api/points/add/` | Добавить точку |
| `DELETE` | `/api/points/<id>/delete/` | Удалить точку |

### Параметры `/api/predict/`

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
