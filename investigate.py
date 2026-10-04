import sqlite3

DB_PATH = "data/bot.db"
# ID пользователя с вашего скриншота
TARGET_USER_ID = 509318569 

def main():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    print(f"=== ИСТОРИЯ БАЛАНСА ПОЛЬЗОВАТЕЛЯ {TARGET_USER_ID} ===")
    try:
        cursor.execute(
            """
            SELECT created_at, amount, new_balance, reason, admin_id 
            FROM balance_history 
            WHERE user_id = ? 
            ORDER BY created_at DESC 
            LIMIT 15
            """, 
            (TARGET_USER_ID,)
        )
        rows = cursor.fetchall()
        
        if not rows:
            print("История баланса пуста.")
        else:
            print(f"{'Дата':<22} | {'Изменение':<10} | {'Итог':<10} | {'Причина':<15} | {'Админ'}")
            print("-" * 75)
            for r in rows:
                date, amount, new_balance, reason, admin_id = r
                admin = admin_id if admin_id else "Система/Бот"
                print(f"{date:<22} | {amount:<10} | {new_balance:<10} | {reason:<15} | {admin}")
                
    except Exception as e:
        print(f"Ошибка чтения базы: {e}")
        
    conn.close()

if __name__ == "__main__":
    main()
