# Настройка GitLab CI/CD для ЭЛОУ-АВТ Digital Twin

Этот документ описывает, что нужно настроить в GitLab (`Settings → CI/CD →
Variables`), почему пайплайн устроен именно так, и какие решения были
приняты на основе реального состояния этого репозитория, а не шаблона.

> **Важно про текущий репозиторий**: `git remote` этого проекта указывает на
> GitHub (`github.com/Atamik03/ItcampMainRepo`), не на GitLab. `.gitlab-ci.yml`
> написан и локально проверен (синтаксис, реальные команды инструментов),
> но **никогда не запускался как настоящий GitLab-пайплайн** — для этого
> репозиторий нужно перенести на GitLab (или зеркалировать) и подключить
> раннеры, как описано в `docs/gitlab-runner.md`.

## 1. Архитектура pipeline

```text
git push
   │
   ▼
validate   — синтаксис .gitlab-ci.yml (GitLab CI Lint API) + docker-compose.yml
   │
   ▼
lint       — frontend-typecheck (tsc -b), frontend-eslint         ┐
   │                                                                │ параллельно
security   — bandit, semgrep, hadolint, trivy-fs                  │
   │                                                                │
test       — backend-tests (pytest)                                ┘
   │
   ▼
build      — build-backend, build-frontend-build, build-frontend-nginx   (параллельно между собой)
             каждый: docker build → Trivy image scan → Docker Scout → docker push
             (push — последний шаг; если скан не прошёл, push не происходит)
   │
   ▼
deploy     — docker compose pull && up -d на deploy-раннере (только default branch автоматически)
   │
   ▼
verify     — healthcheck всех сервисов + smoke-тесты API + regression-проверка 3D-модели
   │
   ▼
Приложение доступно разработчику для тестирования на http://<DEPLOY_HOST>:8080

rollback   — отдельный manual job, возврат на последний известный рабочий SHA
```

**Почему именно так, а не буквально по диаграмме из ТЗ:**
- `lint` и `security`/`test` идут параллельно (через стадии, но без `needs`
  цепочки между ними) — они независимы, не нужно ждать линт перед
  security-сканами.
- `build` использует `needs:` на ВСЕ джобы lint+security+test, а не только
  на предыдущую стадию — гарантирует, что образ не соберётся, если хоть
  одна проверка не прошла, даже если стадии физически идут параллельно.
- Deploy на каждый push в feature-ветку **не выполняется автоматически** —
  в репозитории нет инфраструктуры для динамических per-branch окружений
  (один `docker-compose.yml`, один хост), поэтому автодеплой каждого пуша
  каждой ветки на общий хост означал бы, что ветки разработчиков A, B, C
  постоянно перетирают друг друга. Deploy автоматический только на default
  branch; из любой другой ветки/MR доступен как ручной (`when: manual`) —
  разработчик может сознательно продеплоить свою ветку на общее
  окружение для теста.
- Production вообще не описан — в репозитории нет отдельной
  production-инфраструктуры (только один документированный
  deployment-таргет, трактуемый здесь как development/staging). Добавлять
  вымышленный production-стейдж не стал.

## 2. GitLab CI/CD Variables

Добавить в `Settings → CI/CD → Variables`. Ничего из этого не должно
попадать в репозиторий.

### Реестр образов (обычно не нужно задавать вручную)

| Переменная | Назначение | Protected | Masked | Env-specific |
|---|---|---|---|---|
| `CI_REGISTRY`, `CI_REGISTRY_IMAGE`, `CI_REGISTRY_USER`, `CI_REGISTRY_PASSWORD` | Auto-provided GitLab'ом при включённом Container Registry проекта. Указывать вручную не нужно — pipeline использует их как есть. | — | — | — |

### Docker Scout

| Переменная | Назначение | Protected | Masked | Env-specific |
|---|---|---|---|---|
| `DOCKERHUB_USER` | Docker ID для аутентификации Docker Scout (сервис Docker, отдельный от GitLab Registry). | да | нет | нет |
| `DOCKERHUB_TOKEN` | Personal Access Token Docker Hub (не пароль!) с минимально нужными правами. | да | да | нет |

### Секреты приложения (используются джобом `deploy`)

Точное соответствие `./secrets/*.txt`, которые `start.sh`/`start.ps1`
генерируют локально (см. `README.md` «Docker Secrets: как это устроено») —
но в CI они **не генерируются заново на каждый деплой**, а берутся из
GitLab-переменных постоянными значениями. Причина: `start.sh` намеренно
никогда не перегенерирует уже существующий пароль — если бы CI делал это
каждый раз, Postgres, уже инициализированный со старым паролем в
`./data/postgres` на deploy-хосте, перестал бы принимать подключения после
следующего же деплоя. Сгенерировать значения один раз так же, как это
делает `start.sh` (см. его код — `openssl rand -base64 48 | tr -d ...`),
и сохранить в переменные ниже.

| Переменная | Назначение | Protected | Masked | Env-specific |
|---|---|---|---|---|
| `DEPLOY_POSTGRES_PASSWORD` | Пароль Postgres на deploy-окружении. | да | да | да (environment: development) |
| `DEPLOY_REDIS_PASSWORD` | Пароль Redis на deploy-окружении. | да | да | да |
| `DEPLOY_ELOU_AUTH_SECRET` | Секрет подписи JWT-токенов (≥32 символа), см. `README.md` «Конфигурация и секреты». | да | да | да |

### Несекретная конфигурация приложения

| Переменная | Назначение | Protected | Masked | Env-specific |
|---|---|---|---|---|
| `POSTGRES_USER` | Имя пользователя Postgres (не секрет). | нет | нет | да |
| `POSTGRES_DB` | Имя базы данных (не секрет). | нет | нет | да |
| `ELOU_CORS_ORIGINS` | Разрешённые origin для CORS на deploy-окружении (например `http://<DEPLOY_HOST>:8080`). | нет | нет | да |

### Deploy-раннер

| Переменная | Назначение | Protected | Masked | Env-specific |
|---|---|---|---|---|
| `DEPLOY_HOST` | Хост/IP deploy-раннера — используется только для отображения ссылки в GitLab Environment (`environment: url:`), не для SSH (раннер — это и есть хост, см. `docs/gitlab-runner.md`). | нет | нет | да |
| `DEPLOY_PATH` | Постоянный каталог на deploy-раннере, где живёт стек (например `/srv/elou-avt`) — независим от эфемерного workspace джоба. | нет | нет | да |

### Параметризация severity policy (необязательно, есть значения по умолчанию в `.gitlab-ci.yml`)

| Переменная | Назначение | Значение по умолчанию |
|---|---|---|
| `TRIVY_SEVERITY` | Уровни, которые Trivy считает блокирующими. | `HIGH,CRITICAL` |
| `BANDIT_SEVERITY_LEVEL` | Порог severity для Bandit gate. | `medium` |
| `BANDIT_CONFIDENCE_LEVEL` | Порог confidence для Bandit gate. | `medium` |

## 3. Security policy — что блокирует pipeline, что нет

Политика ниже — не взята бездумно из примера в ТЗ, а выведена из реального
прогона каждого инструмента против этого репозитория (см. историю сессии:
каждый инструмент запускался вживую через `docker run` против настоящего
кода до того, как политика была зафиксирована).

| Инструмент | Blocking | Warning/informational | Обоснование |
|---|---|---|---|
| **Bandit** | `--severity-level medium --confidence-level medium` (medium+/medium+) | Low severity | На момент написания: 0 Medium/High, 13 Low — все проверены вручную (defensive `except: pass` паттерны, ложные срабатывания на "pass" в именах настроек типа `min_pass_score`, легитимный `assert`). 7 реальных находок `B608` (SQL-инъекция через f-string) были не ложными тревогами, а безопасными паттернами (имена колонок/таблиц берутся из фиксированных allow-list constants в коде, не из пользовательского ввода) — помечены `# nosec B608` с объяснением НА КАЖДОЙ строке в коде, а не скопом всего файла. |
| **Semgrep** | Любая находка (`--error`) | — | На момент написания: 0 находок. Единственная находка, встреченная при разработке этой политики (`wildcard-postmessage-configuration` в `FieldOperatorScreen.tsx`, 3 места), была архитектурно обоснованной (sandboxed iframe без `allow-same-origin` получает opaque origin, поэтому `postMessage(..., '*')` — единственный рабочий вариант) и подавлена через `// nosemgrep: <rule-id>` с комментарием прямо в коде, не через общий ignore-файл. |
| **ESLint** | Любая находка severity `error` | severity `warning` | 0 errors / 85 warnings на момент написания (в основном `react-refresh/only-export-components` — DX-подсказка, не баг). ESLint в проекте не было вообще до этой сессии — конфиг (`elou_avt_web/eslint.config.js`) добавлен минимально, по образцу официального Vite React+TS шаблона, без изобретения собственных строгих правил. |
| **Hadolint** | Любая находка (после применения `.hadolint.yaml`) | — | 1 задокументированное исключение (`DL3008` — незапиненные версии `apt` для `build-essential`/`gcc` в builder-стадии backend; см. комментарий в `.hadolint.yaml` — эти пакеты не попадают в финальный образ, а точный пин apt-версий на Debian slim исторически хрупок, т.к. Debian быстро ротирует старые версии из зеркал). |
| **Trivy (fs + image)** | `HIGH,CRITICAL` (после применения `.trivyignore`) | `MEDIUM`/`LOW` | Реальные CVE в текущих закреплённых по digest базовых образах (постоянно меняющаяся вещь — см. ниже). `apt-get upgrade`/`apk upgrade` добавлены в Dockerfile'ы backend/frontend-nginx и реально закрывают все находки, для которых патч уже опубликован в репозитории пакетов ОС. Оставшиеся 17 (backend) + ~45 (frontend-build) записей в `.trivyignore` — **не bulk-suppression**, каждая проверена по полю `Status` реального скана (`affected`/`fix_deferred` = патча нет вообще нигде, не только в текущем образе). |
| **Docker Scout** | `docker scout cves --exit-code --only-severity critical,high` | — | Не удалось живьём проверить в этой сессии — требует Docker Hub credentials, которых нет (см. §2 выше). Синтаксис команды подтверждён (`docker scout cves --help`), но реальный прогон против настоящих образов не выполнялся. |
| **pytest (backend)** | Любой упавший тест | — | 304/304 тестов проходят на момент написания. |

### Про Trivy CVE в базовых образах — честно

При разработке этой политики реальный скан нашёл HIGH/CRITICAL CVE во всех
трёх собираемых образах, используя АКТУАЛЬНЫЕ на тот момент digest-пины
(`scripts/pin-digests.sh` подтвердил — «up to date», новее пока не
опубликовано). Это нормальная, ожидаемая ситуация для любого реального
проекта — апстрим-мейнтейнеры образов не пересобирают их мгновенно при
публикации каждой CVE. `.trivyignore` — не отговорка, а механизм: каждая
запись в нём подтверждена по-отдельности через поле `Status` реального
JSON-отчёта Trivy, что патча нет вообще нигде на момент проверки
(`affected`/`fix_deferred`), а не просто «Trivy ругается, надоело чинить».
Пересматривать этот файл нужно при каждом реальном обновлении digest'ов
(`scripts/pin-digests.sh` перестаёт говорить «up to date») — подробнее в
шапке самого `.trivyignore`.

## 4. Версионирование образов

Каждый образ тегируется `$CI_COMMIT_SHA` (полный SHA коммита, не короткий) —
`latest` нигде не используется как единственный идентификатор. Деплой
всегда явно указывает, какой SHA сейчас развёрнут (файл
`$DEPLOY_PATH/.last_healthy_sha` на deploy-раннере, обновляется только
после успешного прохождения `verify`).

## 5. Reports и artifacts

| Инструмент | Формат | GitLab Security-интеграция |
|---|---|---|
| Semgrep | `--gitlab-sast-output` / `--gitlab-secrets-output` | Нативная — `artifacts.reports.sast` / `secret_detection`, отображается в MR widget и Security Dashboard. |
| Trivy (fs) | `--template "@/contrib/gitlab.tpl"` | Нативная — `artifacts.reports.dependency_scanning`. |
| Trivy (image) | `--template "@/contrib/gitlab.tpl"` | Нативная — `artifacts.reports.container_scanning`. |
| Bandit | JSON (`bandit -f json`) | **Нет** нативной GitLab SAST-схемы для Bandit из коробки. Полный вывод виден в Job Log (`-f screen`: путь к файлу, строка, правило, severity, confidence, описание) + JSON сохраняется как обычный artifact. Более глубокая интеграция потребовала бы отдельного конвертера JSON→GitLab SAST-схема — не реализовано в этой сессии, задокументированное ограничение. |
| Hadolint | текстовый вывод в Job Log | Нет built-in GitLab-схемы для Dockerfile-линтеров; вывод по каждому из трёх Dockerfile печатается отдельным блоком в логе. |
| Docker Scout | текстовый вывод `docker scout cves` в Job Log | Официальной прямой GitLab-интеграции (в отличие от Trivy) на момент написания нет; результаты видны в логе джоба. |

## 6. Что ещё нужно сделать перед первым реальным запуском

1. Перенести/зеркалировать репозиторий на реальный GitLab-инстанс (сейчас
   он на GitHub — см. предупреждение в начале документа).
2. Настроить раннеры — см. `docs/gitlab-runner.md`.
3. Заполнить переменные из §2 выше.
4. Прогнать пайплайн один раз вручную (`web` trigger) и убедиться, что
   `yaml-and-compose-validate` проходит через реальный GitLab CI Lint API
   (в этой сессии проверялась только локальная YAML-валидность и логика —
   реального ответа от `POST /ci/lint` получено не было, т.к. нет доступа
   к настоящему GitLab-проекту).
5. Прогнать `deploy` в первый раз вручную и убедиться, что
   `.last_healthy_sha` создался — до этого `rollback` будет честно
   отказывать («нечего откатывать»), это ожидаемо для самого первого деплоя.
