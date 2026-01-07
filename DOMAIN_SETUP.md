# Подключение сервиса к домену arbitron.dev

## Быстрый старт

Добавь в `docker-compose.yml`:

```yaml
services:
  my-service:
    image: my-image
    networks:
      - traefik-public
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.НАЗВАНИЕ.rule=Host(`arbitron.dev`) && PathPrefix(`/ПУТЬ`)"
      - "traefik.http.routers.НАЗВАНИЕ.entrypoints=web"
      - "traefik.http.services.НАЗВАНИЕ.loadbalancer.server.port=ПОРТ"

networks:
  traefik-public:
    external: true
```

**Замени:**

- `НАЗВАНИЕ` — уникальное имя роутера (например: `api`, `bot`, `admin`)
- `/ПУТЬ` — путь URL (например: `/api`, `/webhook`, `/admin`)
- `ПОРТ` — порт контейнера (например: `8080`, `3000`)

---

## Примеры

### 1. API сервис на `arbitron.dev/api`

```yaml
services:
  api:
    image: my-api:latest
    networks:
      - traefik-public
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.api.rule=Host(`arbitron.dev`) && PathPrefix(`/api`)"
      - "traefik.http.routers.api.entrypoints=web"
      - "traefik.http.services.api.loadbalancer.server.port=8080"

networks:
  traefik-public:
    external: true
```

### 2. С удалением префикса (StripPrefix)

Если сервис ожидает `/` вместо `/api`:

```yaml
labels:
  - "traefik.enable=true"
  - "traefik.http.routers.api.rule=Host(`arbitron.dev`) && PathPrefix(`/api`)"
  - "traefik.http.routers.api.entrypoints=web"
  - "traefik.http.services.api.loadbalancer.server.port=8080"
  - "traefik.http.middlewares.api-strip.stripprefix.prefixes=/api"
  - "traefik.http.routers.api.middlewares=api-strip"
```

### 3. Telegram Webhook на `arbitron.dev/webhook/BOT_TOKEN`

```yaml
services:
  telegram-bot:
    image: my-bot:latest
    networks:
      - traefik-public
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.tgbot.rule=Host(`arbitron.dev`) && PathPrefix(`/webhook`)"
      - "traefik.http.routers.tgbot.entrypoints=web"
      - "traefik.http.services.tgbot.loadbalancer.server.port=8000"

networks:
  traefik-public:
    external: true
```

### 4. Корень домена `arbitron.dev/`

```yaml
labels:
  - "traefik.enable=true"
  - "traefik.http.routers.main.rule=Host(`arbitron.dev`)"
  - "traefik.http.routers.main.entrypoints=web"
  - "traefik.http.services.main.loadbalancer.server.port=3000"
  - "traefik.http.routers.main.priority=1" # низкий приоритет
```

---

## Проверка

```bash
# Список роутеров
curl -s http://localhost:3081/api/http/routers | jq

# Тест сервиса
curl https://arbitron.dev/ПУТЬ
```

---

## Архитектура

```
Internet → Cloudflare → Tunnel → Traefik (:3080) → Docker контейнеры
                                    ↓
                              auto-discovery
                              через labels
```

---

## Частые ошибки

| Ошибка               | Решение                                            |
| -------------------- | -------------------------------------------------- |
| 404 Not Found        | Проверь что контейнер в сети `traefik-public`      |
| 502 Bad Gateway      | Проверь порт в labels (`loadbalancer.server.port`) |
| Сервис не появляется | Добавь `traefik.enable=true`                       |
| Конфликт роутеров    | Используй уникальные имена роутеров                |

---

## SSH доступ к серверу

```bash
sshpass -p 'wow2045606' ssh server@192.168.1.147
```

## Управление Traefik

```bash
# Логи
docker logs traefik -f

# Перезапуск
cd ~/traefik && docker compose restart

# Dashboard
http://SERVER_IP:3081
```
