import sqlite3

DB_PATH = "data/bot.db"

def main():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # То, что ошибочно показывает админка (всё в кучу)
    cursor.execute("SELECT SUM(amount) FROM balance_history")
    total_bug = cursor.fetchone()[0] or 0

    # То, что пользователи потратили на генерации
    cursor.execute("SELECT SUM(amount) FROM balance_history WHERE amount < 0")
    total_spent = cursor.fetchone()[0] or 0

    # ВАШИ РЕАЛЬНЫЕ ПОПОЛНЕНИЯ (ПРИБЫЛЬ)
    cursor.execute("SELECT SUM(amount) FROM balance_history WHERE amount > 0 AND reason != 'generation'")
    total_income = cursor.fetchone()[0] or 0

    print("=== АНАЛИЗ ФИНАНСОВ ===")
    print(f"Сумма, которую ошибочно выводит админка: {total_bug} ₽")
    print(f"Пользователи потратили на генерации: {total_spent} ₽")
    print(f"💰 ВАШИ РЕАЛЬНЫЕ ПОПОЛНЕНИЯ (ПЛЮС): {total_income} ₽")

    conn.close()

if __name__ == "__main__":
    main()
