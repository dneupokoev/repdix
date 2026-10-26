# docker/dockerfiles/redis.dockerfile
FROM redis:7-alpine

# Копируем конфигурационный файл
COPY ./dockerfiles/redis.conf /usr/local/etc/redis/redis.conf

# Запускаем Redis с конфигурацией
CMD ["redis-server", "/usr/local/etc/redis/redis.conf"]