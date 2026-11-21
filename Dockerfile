FROM python:3.13-slim
WORKDIR /bot
COPY . /bot/
RUN python3 -m compileall -q /bot
ENTRYPOINT ["python3", "-u", "/bot/bot.py"]
