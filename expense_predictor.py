import sqlite3
import pandas as pd
import numpy as np
from sklearn.linear_model import LinearRegression

def predict_next_month_expense(user_id):
    conn = sqlite3.connect("database.db")

    query = """
    SELECT expense_date, amount FROM expenses
    WHERE user_id = ?
    """

    df = pd.read_sql_query(query, conn, params=(user_id,))
    conn.close()

    if df.empty:
        return 0

    df['expense_date'] = pd.to_datetime(df['expense_date'])
    df['month'] = df['expense_date'].dt.to_period('M')

    monthly_data = df.groupby('month')['amount'].sum().reset_index()

    if len(monthly_data) < 2:
        return float(monthly_data['amount'].iloc[-1])

    monthly_data['month_number'] = np.arange(len(monthly_data))

    X = monthly_data[['month_number']]
    y = monthly_data['amount']

    model = LinearRegression()
    model.fit(X, y)

    next_month = np.array([[len(monthly_data)]])
    prediction = model.predict(next_month)

    return round(float(prediction[0]), 2)