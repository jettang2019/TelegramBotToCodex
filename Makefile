.PHONY: run start stop restart status logs debug debug-start

run:
	./scripts/service.sh run

start:
	./scripts/service.sh start

stop:
	./scripts/service.sh stop

restart:
	./scripts/service.sh restart

status:
	./scripts/service.sh status || true

logs:
	./scripts/service.sh logs

debug:
	LOG_LEVEL=DEBUG ./scripts/service.sh run

debug-start:
	LOG_LEVEL=DEBUG ./scripts/service.sh start
