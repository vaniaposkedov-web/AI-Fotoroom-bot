import sqlite3

DB_PATH = "data/bot.db"

def main():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Смотрим, что там было записано
    cursor.execute("SELECT value FROM app_settings WHERE key='revenue_manual_correction'")
    row = cursor.fetchone()
    current_val = row[0] if row else "Пусто"
    print(f"🕵️‍♂️ Найдена ручная корректировка: {current_val}")
    
    if current_val != "Пусто" and current_val != "0":
        # Сбрасываем значение на 0
        cursor.execute("UPDATE app_settings SET value = '0' WHERE key = 'revenue_manual_correction'")
        conn.commit()
        print("✅ Гигантский минус успешно удален! Корректировка сброшена на 0.")
    else:
        print("Минусов не найдено, все чисто.")
        
    conn.close()

if __name__ == "__main__":
    main()
