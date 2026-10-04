import sqlite3

DB_PATH = "data/bot.db"

def main():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # 1. Сначала просто найдем и покажем вам эту транзакцию
    cursor.execute("SELECT id, user_id, amount, created_at FROM balance_history WHERE reason = 'admin_edit' AND amount < 0")
    rows = cursor.fetchall()
    
    if not rows:
        print("Минусовых редактирований не найдено.")
    else:
        print("🕵️‍♂️ Найдены минусовые транзакции админа (они портили стату):")
        for r in rows:
            print(f"ID записи: {r[0]} | ID юзера: {r[1]} | Сумма: {r[2]} | Дата: {r[3]}")
            
        # 2. Переименовываем их, чтобы админка перестала их считать
        cursor.execute("UPDATE balance_history SET reason = 'system_correction' WHERE reason = 'admin_edit' AND amount < 0")
        conn.commit()
        print(f"\n✅ Успешно скрыто транзакций: {cursor.rowcount}. Обновите страницу админки!")
        
    conn.close()

if __name__ == "__main__":
    main()
