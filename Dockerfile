FROM python:3.13-slim
WORKDIR /bot
COPY mechfight.py world.py bot.py grid.bin ./
RUN python3 -m compileall -q /bot
ENTRYPOINT ["python3", "-u", "/bot/bot.py"]
