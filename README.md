# ЭЛОУ-АВТ Digital Twin

Цифровой двойник установки ЭЛОУ-АВТ (электрообессоливающая установка +
атмосферно-вакуумная трубчатка) для тренировки операторов: физическое ядро
симуляции процесса, HMI/SCADA-мнемосхема, LMS (курсы, практика, экзамены,
ИИ-разбор ошибок) и 3D-экран полевого оператора.

## Оглавление

1. [Состав проекта](#состав-проекта)
2. [Запуск через Docker (рекомендуется)](#запуск-через-docker-рекомендуется)
3. [Запуск без Docker](#запуск-без-docker-лёгкий-локальный-dev-цикл)
4. [Демо-вход](#демо-вход)
5. [API](#api)
6. [Конфигурация и секреты](#конфигурация-и-секреты)
7. [Docker: архитектура и модель безопасности](#docker-архитектура-и-модель-безопасности)
8. [База данных](#база-данных)
9. [Физическое расчётное ядро](#физическое-расчётное-ядро)
10. [Схемы P&ID и дубли ID](#схемы-pid-и-дубли-id)
11. [Проверка / тесты](#проверка--тесты)
12. [CI/CD (GitHub Actions)](#cicd-github-actions)
13. [Диагностика проблем](#диагностика-проблем)
14. [История изменений](#история-изменений)

---

## Состав проекта

- `elou_avt_twin/` — Python Digital Twin + FastAPI REST/WebSocket backend.
- `elou_avt_web/` — React HMI (Vite + React Flow): технологическая схема, телеметрия, управление, LMS, 3D-экран полевого оператора.
- `docker-compose.yml`, `docker/`, `start.sh`/`start.ps1` — контейнеризированный стек (backend + Postgres + Redis + nginx) — основной, рекомендуемый способ запуска.
- `START_ALL.bat`, `elou_avt_twin/run_backend.bat` — более лёгкий локальный запуск без Docker (venv + `npm run dev`, SQLite вместо Postgres, живой HMR фронтенда).

## Запуск через Docker (рекомендуется)

Требуется только Docker Desktop (Windows/macOS) или Docker Engine (Linux) — не
нужны ни Python, ни Node на хосте, всё собирается внутри контейнеров.

```bash
./start.sh        # Linux / macOS / Git Bash на Windows
```
```powershell
.\start.ps1        # Windows PowerShell
```

Одна идемпотентная команда — можно перезапускать сколько угодно раз, ничего
не сломает и не потрёт существующие данные/секреты. Она сама:

1. проверяет, что Docker Desktop/Docker Engine запущен и `docker compose` (v2) доступен;
2. создаёт `./data/postgres`, если её ещё нет;
3. создаёт `.env` из `.env.example`, если `.env` ещё нет (несекретные настройки: имя БД, CORS-origins и т.п.) — вручную создавать `.env` не нужно;
4. генерирует `./secrets/*.txt` (пароли Postgres/Redis, auth-секрет), если их ещё нет — **никогда не перегенерирует существующие**, иначе Postgres, уже инициализированный со старым паролем, откажет в доступе;
5. закрепляет базовые образы по digest (`scripts/pin-digests.*`), если это ещё не сделано;
6. собирает образы (`docker compose build`);
7. поднимает стек (`docker compose up -d`);
8. ждёт, пока `db`, `redis`, `backend`, `frontend-nginx` не станут `healthy` (таймаут 180 с, с понятной ошибкой и подсказкой в какие логи смотреть, если что-то не поднялось);
9. печатает адрес приложения — `http://localhost:8080`.

**Проверить состояние:**
```bash
./status.sh      # или .\status.ps1
```
Показывает `docker compose ps`, health каждого сервиса и пробует достучаться до `http://localhost:8080/health`.

**Остановить:**
```bash
./stop.sh              # или .\stop.ps1            -- данные сохраняются
./stop.sh --purge       # или .\stop.ps1 -Purge     -- также удаляет volume redis-data/frontend-dist
```
Данные Postgres (`./data/postgres`, bind mount на хост) этими скриптами
никогда не удаляются — если действительно нужно стереть и их, сделайте это
осознанно вручную (`rm -rf ./data/postgres`).

### Предварительные требования

**Windows 11:** Docker Desktop с бэкендом WSL2 (стоит по умолчанию при
обычной установке). Больше ничего ставить не нужно.

**Linux:** Docker Engine + Compose plugin (`docker compose version` должен
показать v2):
```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER"   # чтобы не писать sudo перед каждой командой
# перелогиниться (или newgrp docker), чтобы членство в группе применилось
```

**Разница с Windows/Docker Desktop, о которой стоит знать:** на «чистом»
Linux-хосте (не в VM Docker Desktop) требования безопасности из раздела
[«Docker: архитектура и модель безопасности»](#docker-архитектура-и-модель-безопасности)
отрабатывают полностью и без оговорок — seccomp, AppArmor (или SELinux на
Fedora/RHEL/CentOS), права на `/var/lib/docker` и аудит через `auditd`
доступны напрямую на уровне ядра хоста, а не через прослойку WSL2.

- **AppArmor vs SELinux.** На Debian/Ubuntu активен AppArmor — `security_opt:
  apparmor=docker-default` в `docker-compose.yml` работает как есть. На
  Fedora/RHEL/CentOS вместо AppArmor обычно используется SELinux — на такой
  системе замените `apparmor=docker-default` на `label=type:container_t` и
  убедитесь, что Docker вообще собран с поддержкой SELinux
  (`docker info | grep -i selinux`).
- **`/var/lib/docker`.** На настоящем Linux-хосте это реальный каталог на
  файловой системе — примените обычные меры: `chmod 700 /var/lib/docker`,
  ограничьте, кто состоит в группе `docker` (членство в ней даёт
  root-эквивалентный доступ), включите файловый аудит: `sudo auditctl -w
  /var/lib/docker -p wa -k docker_fs_watch`.
- **Аудит контейнеров.** `docker compose logs` и `docker events` работают
  одинаково везде; на Linux-хосте дополнительно реально поставить Falco для
  мониторинга поведения контейнеров на уровне ядра — в этом стеке он не
  реализован, это следующий шаг для прод-окружения.

## Запуск без Docker (лёгкий локальный dev-цикл)

Требуется Windows 10/11, Python 3.11+ и Node.js 18+ (npm). Использует SQLite
вместо Postgres и живой Vite dev-сервер (HMR) вместо собранного nginx-бандла —
удобно для быстрой итерации на фронтенде/бэкенде без пересборки Docker-образов,
но без Redis (чат/снапшоты работают только в рамках одного процесса) и без
контейнерного hardening.

### Установка с нуля (после клонирования)
```bat
REM 1. Установка Python-зависимостей бэкенда
cd elou_avt_twin
py -3 -m venv .venv
.venv\Scripts\activate.bat
pip install -r requirements.txt
cd ..

REM 2. Установка зависимостей фронтенда
cd elou_avt_web
npm ci
cd ..

REM 3. Запуск обоих сервисов
START_ALL.bat
```

Либо просто запусти `START_ALL.bat` — он сам создаст venv, поставит зависимости
и запустит backend + web. Обрати внимание: `node_modules/`, `.venv/` и `dist/`
не хранятся в git, поэтому после клонирования **обязательно** выполнить
`pip install -r requirements.txt` и `npm install` (это делает `START_ALL.bat`).

### Запуск
1. Запусти `START_ALL.bat`.
2. Backend будет доступен на `http://127.0.0.1:8000/docs`.
3. Web-интерфейс откроется на `http://localhost:5173`.
4. Для демонстрации аварии используй инжекцию отказа через UI или `POST /failure/{equipment_id}`.

## Демо-вход

На экране входа сохранены кнопки быстрого входа. Пароль каждой демонстрационной
учётной записи совпадает с логином:

- `admin` — администратор;
- `instructor` — инструктор;
- `operator` — консольный оператор;
- `field_operator` — полевой оператор с 3D-экраном.

Это намеренное поведение MVP для показа КТК. Перед размещением вне закрытого
демо-контура смените пароли и задайте переменные окружения из раздела ниже.

## API

- `GET /health`
- `GET /state`
- `GET /alarms`
- `GET /events`
- `GET /score`
- `POST /input`
- `POST /action`
- `POST /scenario/start`
- `POST /scenario/reset`
- `POST /scenario/step`
- `POST /failure/{equipment_id}`
- `WS /ws/simulation`
- Полный список (включая `/auth/*`, `/lms/*`) — интерактивная документация на `/docs` (Swagger UI, работает и через Docker на `http://localhost:8080/docs`).

### Демонстрационный сценарий
1. Запустить систему.
2. Показать технологическую схему (React Flow).
3. Инжектировать отказ насоса.
4. Показать изменение состояния и тревоги.
5. Запустить резервный насос через API/UI-интеграцию.
6. Показать восстановление процесса.

## Конфигурация и секреты

- `ELOU_AUTH_MODE=enabled` — режим по умолчанию; API требует Bearer-токен. `ELOU_AUTH_MODE=disabled` допустим только для локальной диагностики и запрещён при `ELOU_ENV=production`.
- `ELOU_AUTH_SECRET` — секрет подписи токенов длиной не менее 32 символов.
  В локальном режиме (без Docker) он создаётся автоматически в `.auth_secret`;
  в `ELOU_ENV=production` без Docker переменная обязательна. **В Docker-стеке**
  секрет передаётся не переменной окружения, а файлом — `ELOU_AUTH_SECRET_FILE`
  указывает на смонтированный Docker Secret (`./secrets/elou_auth_secret.txt`,
  генерируется `start.sh`/`start.ps1`); `ELOU_AUTH_SECRET` в этом случае не
  задаётся вовсе.
- `ELOU_CORS_ORIGINS` — список разрешённых origins через запятую. По умолчанию
  разрешены только локальные Vite dev/preview адреса.
- Токен WebSocket передаётся через subprotocol, а не в URL. Вход ограничен по
  числу неудачных попыток. Схемы загружаются только из каталога `schemes/`.
- **Хранилище данных**: без Docker используется один файл SQLite
  (`elou_avt_twin/sessions.db`, см. [«База данных»](#база-данных)); в
  Docker-стеке основное хранилище — PostgreSQL (`DATABASE_URL_FILE`), а Redis
  используется для pub/sub чата/снапшотов симуляции и кеша авторизации
  (`REDIS_URL_FILE`). Обе схемы поддерживаются одним и тем же кодом
  (`elou_avt_twin/persistence/db.py`).

### Docker Secrets: как это устроено

Пароли (`POSTGRES_PASSWORD`, `REDIS_PASSWORD`) и `ELOU_AUTH_SECRET` **не**
передаются через переменные окружения контейнеров и не хранятся в `.env`.
Вместо этого используется top-level `secrets:` в `docker-compose.yml`
(Docker Secrets, работает и вне Swarm — Compose монтирует каждый файл
read-only в `/run/secrets/<имя>` внутри контейнера):

| Файл (генерируется `start.sh`/`start.ps1`) | Кто использует | Как |
|---|---|---|
| `secrets/postgres_password.txt` | `db` | `POSTGRES_PASSWORD_FILE` — официальный образ Postgres читает пароль из файла нативно. |
| `secrets/redis_requirepass.conf` | `redis` | Содержит строку `requirepass <пароль>`; `redis.conf` подключает её через `include /run/secrets/redis_requirepass` — `redis.conf` не поддерживает `${VAR}`-подстановку, поэтому пароль никогда не попадает в статический конфиг. |
| `secrets/redis_password.txt` | `redis` (healthcheck) | Healthcheck читает пароль заново при каждом запуске (`$$(cat /run/secrets/redis_password)`), а не подставляет его при парсинге compose-файла — иначе он осел бы в `docker inspect`. |
| `secrets/database_url.txt` | `backend` | `DATABASE_URL_FILE=/run/secrets/database_url` — читается `elou_avt_twin/persistence/db.py`. |
| `secrets/redis_url.txt` | `backend` | `REDIS_URL_FILE=/run/secrets/redis_url` — читается `elou_avt_twin/realtime/redis_bus.py`. |
| `secrets/elou_auth_secret.txt` | `backend` | `ELOU_AUTH_SECRET_FILE=/run/secrets/elou_auth_secret` — читается `elou_avt_twin/auth/store.py`. |

Проверить, что секреты действительно не светятся:
```bash
docker inspect itcamp-1-backend-1 --format '{{range .Config.Env}}{{println .}}{{end}}'
# покажет только *_FILE=/run/secrets/..., ни одного пароля в открытом виде
```

`./secrets/` в `.gitignore` и в `.dockerignore` — никогда не коммитится и не
попадает в build context ни одного образа.

## Docker: архитектура и модель безопасности

| Роль контейнера | Сервис в compose   | Образ                                   | Назначение |
|-------------------------------|--------------------|------------------------------------------|------------|
| `docker-front-nginx`          | `frontend-nginx`   | собирается из `docker/frontend-nginx/`   | Отдаёт собранный SPA на `127.0.0.1:8080`, проксирует REST- и WebSocket-запросы на `backend`. **Единственный** сервис с опубликованным портом на хосте. |
| `docker-js`                   | `frontend-build`   | собирается из `docker/frontend-build/`   | Одноразовая задача: `npm ci && npm run build` для `elou_avt_web/`, пишет `dist/` в общий volume и завершается. |
| `docker-beck`                 | `backend`          | собирается из `docker/backend/`          | Приложение FastAPI (`elou_avt_twin/api_server.py`) под `uvicorn`. |
| `docker-bd`                   | `db`               | `postgres:16-alpine`                     | Основное хранилище данных, доступ через `DATABASE_URL_FILE` (Docker Secret). |
| `docker-redis`                | `redis`            | `redis:7-alpine`                         | Кэш и pub/sub, доступ через `REDIS_URL_FILE` (Docker Secret). |

Все пять сервисов оркеструются корневым `docker-compose.yml`. С хост-машины
доступен только `frontend-nginx` (`http://localhost:8080`, слушает
**только** `127.0.0.1`); все остальные сервисы общаются через
две внутренние Docker-сети (`internal` — для db+redis+backend, `edge` — для
backend+frontend-nginx), других портов наружу не открыто.

### Модель безопасности — по каждому пункту требований

| № | Требование | Где реализовано |
|---|---|---|
| 1 | Непривилегированный пользователь в процессах | `docker/backend/Dockerfile` создаёт `appuser` (uid/gid 10001); `docker/frontend-build/Dockerfile` использует встроенного `node` (uid/gid 1000); `docker/frontend-nginx/Dockerfile` — `nginxinc/nginx-unprivileged` (+ явный `USER 101`); `docker-compose.yml` задаёт `user:` явно для backend/redis/frontend-build. |
| 2 | Минимальная поверхность атаки в финальных образах | Многоступенчатая сборка: `docker/backend/Dockerfile`'s `builder` ставит `build-essential`/`gcc`, но в финальный `python:3.12-slim` переходит только `/opt/venv` и код приложения — компилятор в runtime-образ не попадает. |
| 3 | Минимизация Linux capabilities | Везде `cap_drop: [ALL]`. Только `db` дополнительно получает 5 capabilities — см. ниже, там же честный отчёт об эмпирической попытке их урезать. |
| 4 | Запрет повышения привилегий | `security_opt: ["no-new-privileges:true", ...]` у каждого сервиса. |
| 5 | Фильтрация системных вызовов (seccomp) | `docker/seccomp/hardened.json` на всех 5 сервисах — детали ниже. |
| 6 | Мандатное управление доступом (LSM) | `security_opt: apparmor=docker-default` везде; на Linux с SELinux — см. выше. |
| 7 | Файловая система только для чтения | `backend`, `redis`, `frontend-nginx` и `frontend-build` выставляют `read_only: true` с точечными `tmpfs`. `frontend-build` безопасно сделать read-only, потому что к моменту запуска контейнера `npm run build` уже отработал на этапе `docker build`, а сам контейнер только копирует уже собранный `/app/dist` в volume. |
| 8 | Сегментация сети | `internal` (marked `internal: true`) несёт db+redis+backend; `edge` несёт backend+frontend-nginx. |
| 9 | Секреты никогда не запекаются в образы | Docker Secrets — см. [«Docker Secrets: как это устроено»](#docker-secrets-как-это-устроено). |
| 10 | Защита фронтенда от XSS через заголовки ответа | `docker/frontend-nginx/nginx.conf`: строгий CSP (`script-src 'self'`, без `unsafe-inline`/`unsafe-eval`), `X-Frame-Options`, `Referrer-Policy`, `Permissions-Policy`, `server_tokens off`. Точечное исключение для страницы 3D-модели — см. ниже. |
| 11 | Ограничение потребления ресурсов / защита от DoS | `deploy.resources.limits.{cpus,memory,pids}` у каждого сервиса; `maxmemory`/`allkeys-lru` у Redis; `client_max_body_size` у nginx; лимиты логов везде. |
| 12 | Сканирование на уязвимости | `scripts/scan-images.sh`/`.ps1` (Trivy); `scripts/pin-digests.sh`/`.ps1` (immutable digest). |

**Про `pids` / `init` / `start_period`:** текущая версия Docker Compose
(v5.x) не позволяет одновременно использовать устаревшее top-level поле
`pids_limit` и `deploy.resources.limits` — лимиты процессов заданы как
`deploy.resources.limits.pids` (`db: 200` — Postgres, один OS-процесс на
подключение, нужен запас; `redis: 50`; `backend: 200`; `frontend-build: 50`;
`frontend-nginx: 100`). `init: true` стоит у всех 5 сервисов — корректная
пересылка сигналов и сбор зомби-процессов (особенно важно для `backend`, где
`uvicorn` напрямую является PID 1). `start_period` у healthcheck — `db`: 20с
(initdb на первом старте), `redis`: 10с, `frontend-nginx`: 5с — без этого
окна медленный первый старт мог засчитаться как `unhealthy` раньше времени.

### Postgres: минимизация capabilities — что показала проверка

Проверялось: действительно ли `db` нужны все 5 capabilities (`CHOWN, SETUID,
SETGID, DAC_OVERRIDE, FOWNER`), или часть можно убрать. Проверено эмпирически:

1. Убрали `DAC_OVERRIDE` и `FOWNER`, оставив только `CHOWN, SETUID, SETGID`.
2. Запустили `docker compose up` на **свежем**, ещё не инициализированном `./data/postgres`.
3. Результат — контейнер не поднялся: `chmod: /var/lib/postgresql/data: Operation not permitted`, `find: /var/lib/postgresql/data: Permission denied`.
4. Вернули `DAC_OVERRIDE` и `FOWNER` — с этого момента и первичная инициализация, и рестарты проходят чисто.

Официальный entrypoint образа `postgres` всегда стартует от root (в его
Dockerfile нет непривилегированного `USER`) и на **каждом** старте (не
только при первой инициализации) делает `chmod`/`find` по каталогу данных
перед переключением на пользователя `postgres` через `gosu`. Убрать эти
capabilities без форка официального образа нельзя — все 5 у `db` оставлены,
ослаблений в других сервисах при этом не производилось.

### Три причины, по которым не грузилась 3D-модель «Кабинета полевого оператора»

Найдены и устранены при аудите — все три касаются `docker/frontend-nginx/nginx.conf`
и одной страницы `elou_avt_web/public/avt4_3d_model_v7.html`:

1. **`X-Frame-Options: DENY` / CSP `frame-ancestors 'none'`** блокировали
   собственный `<iframe>` приложения (`FieldOperatorScreen.tsx` встраивает
   `avt4_3d_model_v7.html` в себя же, same-origin). nginx отдавал 200,
   браузер отказывался рендерить фрейм — не видно ни в сети, ни в логах,
   только в консоли браузера. **Фикс:** отдельный `location =
   /avt4_3d_model_v7.html`, переопределяющий `X-Frame-Options: SAMEORIGIN` /
   `frame-ancestors 'self'` только для этой страницы; остальной сайт
   остаётся на `DENY`/`none`.
2. **Тихое зависание `frame()`** — `requestAnimationFrame(frame)`
   перепланирует себя первой строкой, поэтому исключение внутри тела кадра
   не долетает до внешнего `try/catch` (отдельный кадр стека вызовов) — ни
   сигнал готовности, ни ошибка никогда не отправлялись. **Фикс:** тело
   `frame()` обёрнуто в собственный `try/catch`, при сбое один раз
   показывается реальная ошибка вместо бесконечного спиннера.
3. **CSP `script-src 'self'` блокировал вообще все `<script>` на странице**
   — `avt4_3d_model_v7.html` это один самодостаточный файл, ~1700 строк
   инлайнового Three.js плюс инлайновые `onload=`/`onerror=`, без
   `'unsafe-inline'` не выполнялась ни строчка. **Фикс:** в том же
   `location = /avt4_3d_model_v7.html` добавлено `'unsafe-inline'` к
   `script-src` только для этой страницы (статический, собираемый на этапе
   `docker build` файл без пользовательского ввода — модель угроз XSS сюда
   не применима так же, как к остальному SPA).

Проверка после каждого фикса:
```bash
curl -sD- -o /dev/null "http://localhost:8080/avt4_3d_model_v7.html?theme=dark" | grep -i "x-frame\|content-security"
# X-Frame-Options: SAMEORIGIN
# Content-Security-Policy: ...; script-src 'self' 'unsafe-inline'; ...; frame-ancestors 'self'
curl -sD- -o /dev/null "http://localhost:8080/" | grep -i "x-frame\|content-security"
# X-Frame-Options: DENY
# Content-Security-Policy: ...; script-src 'self'; ...; frame-ancestors 'none'   -- главный сайт не ослаблен
```

Побочная находка при тестировании: именованный volume `frontend-dist`
Docker наполняет из образа только **при первом создании volume** — повторная
пересборка `frontend-build` молча не долетала до раздачи. Исправлено в
`docker/frontend-build/Dockerfile`: свежая сборка копируется в
`/app/dist-baked` (путь вне volume-маунта), а `CMD` контейнера при каждом
запуске явно копирует его в `/app/dist` — так что каждый `docker compose up
-d frontend-build` реально обновляет раздачу.

### Пояснения по seccomp-профилю (`docker/seccomp/hardened.json`)

Построен по образцу собственного default-профиля Docker: `defaultAction:
SCMP_ACT_ERRNO` (запрет по умолчанию) с одним большим allow-list'ом (~340
syscalls), покрывающим то, что нужно uvicorn (Python/asyncio), Node, nginx,
Postgres, Redis. Намеренно **не включены** (как и в настоящем
default-профиле Docker): `ptrace`, `mount`, `umount2`, `pivot_root`, `reboot`,
`swapon`, `swapoff`, `kexec_load`, `init_module`, `delete_module`,
`finit_module`, `add_key`, `request_key`, `keyctl`, `bpf`, `perf_event_open`,
`process_vm_readv`, `process_vm_writev`, `personality`, `acct`, `quotactl`,
`nfsservctl`, `open_by_handle_at`, `uselib`, `userfaultfd`, `ustat`,
`vhangup`, `syslog`, `settimeofday`, `clock_settime`, `clock_adjtime`,
`sethostname`, `setdomainname`. Компромисс: список намеренно ближе к
чуть более разрешительному краю «жёстко, но без опасного списка выше», а не
минимальный — слишком строгий профиль ломает приложение непредсказуемо.
Проверено вживую: весь стек работает под этим профилем без единой ошибки
`Operation not permitted`/`Function not implemented` в логах.

## База данных

> Хранилище работает на двух движках через общий DB-API-совместимый слой
> `persistence/db.py` — какой используется, определяется способом запуска:
>
> - **Docker (рекомендуемый способ)** — основное хранилище **PostgreSQL**
>   (сервис `db`, строка подключения через `DATABASE_URL_FILE`, Docker
>   Secret). Схема для Postgres — отдельная константа `_SCHEMA_POSTGRES` в
>   каждом из четырёх модулей ниже (`AUTOINCREMENT` → `GENERATED ALWAYS AS
>   IDENTITY`, без `PRAGMA`).
> - **Локальный запуск без Docker** — один файл **SQLite**,
>   `elou_avt_twin/sessions.db`.
>
> Структура таблиц ниже одинаково описывает оба движка — бизнес-логика
> хранилищ (`?`-плейсхолдеры, `ON CONFLICT ... DO UPDATE`) не знает, с каким
> движком работает.

Все четыре хранилища приложения открывают собственные соединения (через
`persistence/db.py`):

| Модуль | Файл | Назначение |
|---|---|---|
| Авторизация (RBAC) | `auth/store.py` | роли, права, пользователи |
| Тренировки и симуляция | `persistence/session_store.py` | сессии, действия, снапшоты, аварии, ошибки, ИИ-классификация |
| LMS (обучение, кабинеты) | `lms/store.py` | курсы, модули, группы, компетенции, прогресс, уведомления |
| LMS (авторство и контроль) | `lms/content_store.py` | уроки, тесты, задания, сценарии, оценки, журналы |

Режимы SQLite, общие для всех хранилищ **при запуске без Docker**
(`persistence/db.py` применяет их только для sqlite-диалекта — в Postgres
эквивалентное поведение уже штатное): `PRAGMA journal_mode=WAL`, `PRAGMA
busy_timeout=5000`, `PRAGMA foreign_keys=ON`.

Схемы P&ID хранятся **не в БД**, а в JSON-файлах `schemes/*.json` (см.
[«Схемы P&ID и дубли ID»](#схемы-pid-и-дубли-id)). Всего пользовательских
таблиц: **30**.

<details>
<summary><b>Полная схема таблиц (раскрыть)</b></summary>

### Авторизация (RBAC) — `auth/store.py`

Авторизация всегда выводится из прав: эффективный набор прав пользователя —
объединение прав всех его ролей.

**`roles`** — `code` (PK), `name`, `description`
**`permissions`** — `code` (PK), `description`
**`role_permissions`** (M:N) — `role_code` FK, `permission_code` FK
**`users`** — `id` (PK), `username` (UNIQUE), `password_hash`, `full_name`, `is_active`, `created_at`
**`user_roles`** (M:N) — `user_id` FK, `role_code` FK

Демо-учётные записи (пароль = логин): `admin`, `instructor`, `operator`, `field_operator`.

### Тренировки и симуляция — `persistence/session_store.py`

Хранилище — **event-sourced**: события только добавляются (append-only), по
ним можно полностью восстановить сессию и выгрузить корпус для офлайн-анализа ИИ.

- **`sessions`** — `id` (UUID), `scenario_id`, `operator_id`, `status` (CREATED→RUNNING→…→COMPLETED), `sim_start`/`sim_end`, `wall_start`/`wall_end`, `scheme_version`, `performance_score`, `qualification`, `ai_verdict` (JSON), `created_at`
- **`actions`** (append-only) — `session_id` FK, `seq`, `sim_time`, `wall_time`, `operator_id`, `equipment_id`, `node_type`, `action_type`, `old_value`/`new_value` (JSON), `source`, `accepted`, `reject_reason`; UNIQUE(session_id, seq)
- **`state_snapshots`** — `session_id` FK, `seq`, `sim_time`, `reason`, `action_id` FK, `pressure`/`temperature`/`levels`/`flows`/`pump_states`/`valve_positions`/`equipment_states`/`controller_states`/`active_alarms`/`active_failures` (все JSON); UNIQUE(session_id, seq)
- **`alarms`** — `session_id` FK, `alarm_id`, `parameter`, `severity`, `actual_value`, `threshold`, `description`, `raised_at`/`acked_at`/`acked_by`/`cleared_at`; UNIQUE(session_id, alarm_id, raised_at)
- **`error_events`** — `session_id` FK, `sim_time`, `action_id` FK, `rule_error_type`, `severity`, `expected_action`, `cause`, `consequence`, `context_snapshot_id` FK, `ai_class`/`ai_confidence`/`ai_reasoning`/`ai_status`
- **`expected_actions`** (ground truth) — `scenario_id`, `equipment_id`, `action_type`, `value`, `deadline_t`, `description`, `consequence`, `weight`; UNIQUE(scenario_id, equipment_id, action_type)
- **`ai_classifications`** (аудит каждого вызова ИИ) — `session_id` FK, `error_event_id` FK, `model`, `prompt_version`, `input_payload` (JSON), `predicted_class`, `confidence`, `reasoning`, `human_correction`, `human_corrected`, `latency_ms`, `created_at`

### LMS (кабинеты и обучение) — `lms/store.py`

- **`lms_groups`** — `id`, `name`, `description`, `course_id`, `instructor_id`, `created_at`
- **`lms_group_members`** (M:N) — `group_id` FK, `user_id` FK
- **`lms_courses`** — `id`, `title`, `description`, `status` (DRAFT/ACTIVE/ARCHIVED), `created_at`
- **`lms_course_modules`** — `id`, `course_id` FK, `kind` (theory/practice/exam), `title`, `description`, `seq`, `content`, `scenario_id`, `practice_task_id`, `published`
- **`lms_competencies`** — `code` (PK), `title`, `description`
- **`lms_user_competencies`** (M:N) — `user_id` FK, `competency_code` FK, `level_percent`, `updated_at`
- **`lms_practice_tasks`** — `id`, `title`, `description`, `scenario_id`, `category`, `difficulty` (EASY/MIDDLE/HARD), `duration_min`, `required_competencies` (JSON), `is_random`, `enabled`
- **`lms_user_progress`** — `user_id` FK, `module_id` FK, `status` (NOT_STARTED/IN_PROGRESS/COMPLETED), `score`, `attempts`, `completed_at`, `last_practice_session_id`
- **`lms_notifications`** — `id`, `user_id` FK, `text`, `kind`, `is_read`, `created_at`
- **`lms_settings`** (key/value) — `key` (PK), `value`
- **`lms_system_log`** — `id`, `timestamp`, `level`, `username`, `message`, `category`

### LMS (авторство и контроль) — `lms/content_store.py`

Все «JSON-колонки» хранятся как текст и десериализуются на уровне приложения.

- **`lms_lessons`** — `id`, `module_id` FK, `title`, `seq`, `blocks` (JSON), `equipment_ids` (JSON), `competency_codes` (JSON), `created_at`
- **`lms_tests`** — `id`, `module_id` FK, `title`, `passing_score`, `attempts`, `retry_required`, `shuffle`, `competency_codes` (JSON), `created_at`
- **`lms_questions`** — `id`, `test_id` FK, `kind` (single/multi/match/sequence/object), `title`, `text`, `seq`, `options` (JSON), `answer` (JSON), `max_score`, `penalty`, `required`, `hint`
- **`lms_training_tasks`** — `id`, `module_id` FK, `title`, `goal`, `scenario_id`, `duration_min`, `initial_state`/`target_state`/`restrictions`/`criteria`/`expected_actions`/`critical_errors`/`competency_codes`/`equipment_ids` (все JSON), `enabled`, `created_at`
- **`lms_scenarios`** — `id`, `module_id` FK, `title`, `description`, `goal`, `status` (DRAFT/REVIEW/PUBLISHED/ARCHIVED), `initial_state`/`events`/`expected_actions`/`success_criteria`/`critical_errors`/`target_state`/`final_state`/`competency_codes`/`equipment_ids` (JSON), `duration_min`, `is_exam`, `created_at`
- **`lms_assessments`** — `id`, `user_id` FK, `module_id` FK, `kind` (test/practice/exam), `test_id`, `task_id`, `scenario_id`, `score`, `max_score`, `passed`, `criteria_scores` (JSON), `errors_count`, `critical_errors_count`, `duration_s`, `answers` (JSON), `feedback_good`/`feedback_bad` (JSON), `session_id`, `started_at`, `finished_at`, `created_at`
- **`lms_action_log`** — журнал действий оператора (append-only, без FK-ограничений)
- **`lms_scada_log`** — журнал взаимодействия с мнемосхемой (append-only, без FK-ограничений)

### Краткая ER-схема

```
roles ──< role_permissions >── permissions
users ──< user_roles >── roles
users ──< lms_group_members >── lms_groups
users ──< lms_user_competencies >── lms_competencies
users ──< lms_notifications
users ──< lms_user_progress >── lms_course_modules
users ──< lms_assessments >── lms_course_modules

lms_courses ──< lms_course_modules
lms_course_modules ──< lms_lessons / lms_tests(──<lms_questions) / lms_training_tasks / lms_scenarios

sessions ──< actions / state_snapshots / alarms / error_events / ai_classifications
error_events ──< ai_classifications
actions ──< state_snapshots.action_id
```

### Резервное копирование и восстановление

**Без Docker (SQLite):** файл `elou_avt_twin/sessions.db` (+ `-wal`/`-shm`
при работающем приложении); для корректной копии — остановить приложение
либо `sqlite3 .backup`/`VACUUM INTO` (безопасно при работающем WAL).

**Docker-стек (PostgreSQL):** данные хранятся на хосте через bind mount
`./data/postgres` (виден напрямую с хост-машины). Бэкап пока стек запущен:
```bash
docker compose exec db sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' > backup.sql
```
Восстановление:
```bash
cat backup.sql | docker compose exec -T db sh -c 'psql -U "$POSTGRES_USER" "$POSTGRES_DB"'
```
Холодная копия (приложение остановлено) — просто скопировать `./data/postgres`.

Схемы P&ID в обоих случаях хранятся в `elou_avt_twin/schemes/` (не в БД).

</details>

## Физическое расчётное ядро

```
elou_avt_twin/
├── calculation_core/         # Слой физических расчётов
│   ├── thermodynamics/       # VLE, Flash (Rachford-Rice), Antoine, Enthalpy, Cp
│   ├── hydraulics/           # Darcy-Weisbach, Valve Flow
│   └── solver/                # Численные решатели (MESH)
├── models/
│   ├── stream.py              # Унифицированная модель потока (Stream)
│   └── base.py                # SimulationState, SimulationConfig
├── equipment/                 # Оборудование со Stream-интерфейсом
│   ├── pump.py, valve.py, heater.py, distillation_column.py, elou.py, ...
├── simulation_core/           # Оркестрация и API
└── tests/                     # Тесты физики
```

Все связи между оборудованием используют объект `Stream` (температура [K],
давление [Pa], массовый/молярный расход, состав, термодинамические свойства,
фазовое состояние). Термодинамика — VLE методом Рачфорда-Райса, K-values на
основе уравнения Антуана, зависимость энтальпии/теплоёмкости/плотности от
температуры и состава. `balance_checker.py` автоматически проверяет
материальный (`ΣIn - ΣOut = dM/dt`) и энергетический (`ΣH_in - ΣH_out + Q + W
= dU/dt`) баланс, а также физические границы (T>0, P>0, сумма долей=1).

**Демонстрация физики:**
```bash
cd elou_avt_twin
python3 demo.py
python3 -m pytest tests/test_rigorous.py -v
```

**Допущения и точки роста (физическое ядро — MVP, код не менялся с последнего
рефакторинга):**
1. Термодинамика — идеальная смесь; для промышленной точности нужна EOS (например, Peng-Robinson) и калибровка на паспортных/лабораторных данных.
2. MESH-решатель колонны — упрощённое разделение; нужен полный метод Ньютона-Рафсона.
3. Гидравлика — требует полноценного решателя для разветвлённых сетей.
4. Нет golden-наборов режимов с проверкой баланса на каждом шаге с заданными допусками.
5. Нет контроля сходимости (максимум итераций, невязки, fallback, статус `non-converged`).
6. Нет property-based тестов физических границ/единиц измерения на широких диапазонах входов.
7. Realtime-оркестрация и вычислительный worker не разделены — тяжёлый solve может блокировать API/WebSocket.
8. Нет детерминированных checkpoint/replay тестов сценариев и версионирования параметров ядра.

Физическое ядро является MVP-моделью. Для промышленного применения
необходима дальнейшая валидация термодинамики и MESH-решателя.

## Схемы P&ID и дубли ID

Схемы автоматически мигрируются к формату `1.2`. Все узлы и связи сохраняются:
первый объект оставляет исходный ID, последующие получают детерминированный
суффикс `__dupN`. Неоднозначные старые связи остаются у первого объекта —
автоматического угадывания и скрытой перепривязки нет.

Повторная безопасная нормализация всех файлов:
```bash
cd elou_avt_twin
python tools/normalize_scheme_ids.py
```

Текущая активная схема задаётся глобальной переменной `scheme_store` в
`api_server.py`; `GET /scheme` возвращает её, `POST /scheme` сохраняет в
файл `schemes/<id>.json` и пересобирает движок.

## Проверка / тесты

```bash
cd elou_avt_twin
python -m pytest -q
python demo.py

cd ../elou_avt_web
npm ci
npm run build
node tools/benchmark_3d_model.mjs public/avt4_3d_model_v7.html
npm run lint
```

## CI/CD (GitHub Actions)

Пайплайн описан в [`.github/workflows/ci.yml`](.github/workflows/ci.yml) и
запускается на каждый push и pull request:

```
validate  ->  lint  ->  security  ->  test  ->  build
```

- **validate** — проверка, что `docker-compose.yml` + `docker-compose.ci.yml` корректно мёржатся (`docker compose config`).
- **lint** — TypeScript typecheck (`tsc -b --noEmit`) и ESLint для `elou_avt_web/`.
- **security** (параллельно с `test`) — Bandit и Trivy filesystem-скан бэкенда, Semgrep (Python/TS/React + security-audit + secrets), Hadolint для всех трёх Dockerfile.
- **test** — `pytest` бэкенда (SQLite/no-op Redis, без внешних сервисов).
- **build** — сборка трёх образов (`backend`, `frontend-build`, `frontend-nginx`) матрицей, Trivy image-скан, и только при успешном скане — push в GitHub Container Registry (`ghcr.io/<owner>/<repo>/<service>:<sha>`). Аутентификация — встроенный `GITHUB_TOKEN`, дополнительные секреты не нужны.

**Что не реализовано:** стадии `deploy`/`verify`/`rollback` (раскатка `docker
compose` на реальный сервер и последующие smoke-тесты) требуют self-hosted
GitHub Actions runner с доступом к целевому хосту — сейчас такого раннера
нет, поэтому эти стадии не подключены. Причины и структура для будущего
переноса задокументированы прямо в шапке `.github/workflows/ci.yml`.

Docker Scout (использовался в прежнем GitLab-пайплайне как второй скан
образов, помимо Trivy) убран — требовал отдельного логина в Docker Hub,
не связанного с публикацией образов в ghcr.io.

## Диагностика проблем

**«Operation not permitted» / «Function not implemented» в логах контейнера**
— почти всегда не хватает syscall в seccomp-профиле:
1. `docker compose logs <service>` — найти ошибку.
2. Временно `security_opt: ["seccomp=unconfined"]` для этого сервиса, `docker compose up -d <service>` — если ошибка исчезла, дело в seccomp.
3. Добавить недостающий syscall в `docker/seccomp/hardened.json`, вернуть прежний `security_opt`, перезапустить.

**«chmod: Operation not permitted» / «find: ... Permission denied» в логах `db`**
— capabilities `db` урезаны сверх того, что нужно официальному образу Postgres; см. [«Postgres: минимизация capabilities»](#postgres-минимизация-capabilities--что-показала-проверка). Верните `DAC_OVERRIDE`/`FOWNER`.

**Порт 8080 уже занят.** Что-то ещё слушает 8080 на хосте. Либо остановите
этот процесс, либо поменяйте `frontend-nginx.ports` в `docker-compose.yml`
(например `"127.0.0.1:8081:8080"`) и обновите `ELOU_CORS_ORIGINS` в `.env`.

**Postgres не принимает пароль после `./start.sh`.** Если `./data/postgres`
уже содержит кластер, инициализированный со СТАРЫМ паролем, а
`secrets/postgres_password.txt` пуст/удалён и был перегенерирован — единственный
сценарий, который `start.sh` не может исправить сам (см. предупреждение,
которое он печатает в этом случае). Либо восстановите старый
`secrets/postgres_password.txt`, либо сотрите `./data/postgres` и дайте
Postgres инициализироваться заново (это удалит данные).

**Docker Desktop подвисает / WSL2 не отвечает (Windows).** Из PowerShell с
правами администратора: `wsl --shutdown`, затем перезапустить Docker Desktop.

**Проблемы с правами/группой `docker` (Linux).** Перелогиньтесь или `newgrp
docker` — членство в группе применяется не мгновенно.

## История изменений

### Рефакторинг MVP: 3D-экран, схемы, безопасность

- **3D-экран полевого оператора**: фон сделан белым, исправлена гонка загрузки Three.js (`defer` вместо `async`), добавлены события готовности/ошибки, геометрии кэшируются, трубы/опоры объединены в `InstancedMesh`, отрисовка ограничена 30 FPS. Benchmark: подготовка сцены 1081→248 мс (в 4.4 раза быстрее), heap −71%, уникальных геометрий −96.6%.
- **Схемы**: формат повышен до `1.2`, дублирующиеся ID узлов/связей получают суффикс `__dupN`, ни один узел/связь не потерян (1038 узлов, 1382 связи во всех 10 файлах после миграции), запись атомарная, выход из каталога `schemes/` запрещён.
- **Стабильность**: route-level lazy loading страниц, React Flow/ECharts в отдельных chunks, WebSocket reconnect с backoff, фоновый поток симуляции через FastAPI lifespan, устранён двойной `DigitalTwin`.
- **Безопасность**: авторизация включена по умолчанию, универсальный demo-секрет заменён на генерируемый локально/обязательный в проде, wildcard CORS заменён allowlist, добавлены HTTP security headers, действия пишутся от аутентифицированного пользователя (не из тела запроса), WebSocket-токен в subprotocol, rate-limit на вход, ECharts обновлён до 6.1.0 (устранены известные уязвимости).

### Аудит и оптимизация кодовой базы (текущая ревизия)

Полный аудит frontend/backend/Docker/тестов/документации, устранение
технического долга без изменения функциональности:

- **Критический баг**: `auth/store.py` итерировал `Cursor` напрямую (старая
  идиома `sqlite3`), которую DB-API шим (`persistence/db.py`) не
  поддерживает — ломало **все** эндпоинты управления ролями в проде
  (`GET/POST/PUT /auth/roles*` → 500). Исправлено (`.fetchall()`),
  подтверждено тестами и на живом стеке.
- Добавлен отсутствовавший `httpx` в зависимости (блокировал сборку части
  тестового набора); удалён неиспользуемый `exceptiongroup` (бэкпорт для
  Python <3.11, проект работает на 3.12/3.14).
- Устранено дублирование `_json`/`_unjson` в трёх store-модулях — вынесено в
  `persistence/db.py`.
- Удалены 17 неиспользуемых импортов, ~76 неиспользуемых файлов
  (`avt.html` — устаревшая копия 3D-визуализации; `visual/` — ~30
  design-референсов, ни разу не загружаемых кодом; разовые debug-дампы;
  устаревший SQLite-бэкап; три дублирующих non-Docker launcher-скрипта).
- Найден и исправлен пробел в `docker/frontend-nginx/nginx.conf` —
  `/openapi.json` не проксировался, из-за чего Swagger UI (`/docs`) не смог
  бы отрендериться в браузере.
- ESLint в `elou_avt_web/` — подтверждено отсутствие конфигурации (не
  добавлялось в рамках аудита, вне объёма «не переписывай ради стиля»).

