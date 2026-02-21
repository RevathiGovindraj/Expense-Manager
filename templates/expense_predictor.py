import sqlite3
import pandas as pd
import numpy as np
from sklearn.linear_model import LinearRegression

def predict_next_month_expense(user_id):
    conn = sqlite3.connect("database.db")

    query = """
    SELECT date, amount FROM expenses
    WHERE user_id = ?
    """

    df = pd.read_sql_query(query, conn, params=(user_id,))
    conn.close()

    # If no data
    if df.empty:
        return 0

    # Convert date column
    df['date'] = pd.to_datetime(df['date'])

    # Group by month
    df['month'] = df['date'].dt.to_period('M')
    monthly_data = df.groupby('month')['amount'].sum().reset_index()

    # If only 1 month data
    if len(monthly_data) < 2:
        return float(monthly_data['amount'].iloc[-1])

    # Convert month into numeric index
    monthly_data['month_number'] = np.arange(len(monthly_data))

    X = monthly_data[['month_number']]
    y = monthly_data['amount']

    # Train Linear Regression model
    model = LinearRegression()
    model.fit(X, y)

    # Predict next month
    next_month = np.array([[len(monthly_data)]])
    prediction = model.predict(next_month)

    return round(float(prediction[0]), 2)