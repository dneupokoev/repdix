# Changelog

## [Unreleased]

### Added
- feat: добавить модуль RBACGuard для контроля доступа к таблицам и полям SQL
- feat: добавить настройку команд бота в telegram_adapter.py
- feat: добавить FSM-регистрацию пользователей и одобрение заявок администраторами
- feat: добавить обработку команды /help в engine.py
- feat: чистый релиз repdix (без секретов)

### Fixed
- fix: исправить логирование, Telegram callback, LLM client и удалить устаревший алиас RBAC
- fix: добавить проверку user_data и уточнить regex для SQL-комментариев
- fix: добавить импорт asyncio в llm_client.py
- fix: заменить прямой доступ к конфигам БД, добавить логирование ошибки и асинхронное закрытие LLM-клиентов
- fix: заменить тройные кавычки в докстринге для устранения SyntaxError
- fix: перевести докстринг в raw-string для устранения SyntaxWarning
- fix: защита users.yaml, уточнение валидатора SQL и рефакторинг форматирования
- fix: устранить противоречия в промптах и multi-statement в init_command MySQL
- fix: усилить правило вызова get_table_schema перед execute_sql

### Changed
- refactor: заменить хранение SQL в истории на last_sqls и обновить методы удаления пользователя и проверки Redis

### Documentation
- docs: обновить комментарии и регулярное выражение для валидации SQL

### Other
- [non-conventional] ch
- [non-conventional] ch
- [non-conventional] ch

# CHANGELOG

