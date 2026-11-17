FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# 只带代码和 schema，本地开发库 solar.db*（含旧模拟数据）不进镜像
COPY app.py schema.sql requirements.txt ./
COPY static ./static
RUN mkdir -p /app/data           # 数据卷挂载点，DB 落这里重建不丢
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
