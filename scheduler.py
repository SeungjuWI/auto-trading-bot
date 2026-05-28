import schedule
import time
from datetime import datetime
from main import run_full_analysis, run_manage_only

def job_manage():
    print(f"\n[{datetime.now()}] 포지션 관리 실행")
    try:
        run_manage_only()
    except Exception as e:
        print(f"포지션 관리 에러: {e}")

def job_full():
    print(f"\n[{datetime.now()}] 전체 분석 실행")
    try:
        run_full_analysis()
    except Exception as e:
        print(f"전체 분석 에러: {e}")

# 매 1시간 포지션 관리
schedule.every(1).hours.do(job_manage)

# 매 4시간 전체 분석 (0, 4, 8, 12, 16, 20시)
schedule.every(4).hours.do(job_full)

# 시작 시 1회 전체 분석 실행
print(f"=== 봇 스케줄러 시작 ({datetime.now()}) ===")
print("스케줄: 매 1시간 포지션 관리 / 매 4시간 전체 분석")
job_full()

while True:
    schedule.run_pending()
    time.sleep(30)
