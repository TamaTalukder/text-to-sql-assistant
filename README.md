# Text-to-SQL Assistant (Northwind)

Ask a business question in plain English and get the answer from a database. The app turns the question into SQL, checks the SQL for safety, runs it, and explains the result in plain language.

**Live demo:** [add your Streamlit link here]

## Why I built this

I am building a portfolio for data analyst roles, and SQL is the skill I use most. I wanted a project where someone without SQL knowledge can ask a business question and trust the answer, so I focused on what makes that safe: checking every query, opening the database read-only, and testing the results on 20 questions with known answers.

Along the way I learned how to build a prompt from a database's real structure, how to handle API rate limits, and how to measure an AI system with a test set instead of trusting it.

## What it does

- Reads the database structure (tables, columns, keys, date ranges) automatically and gives it to the model.
- Uses Google Gemini to write one SQL query for the question.
- Allows only a single `SELECT` statement, and opens the database read-only.
- If the SQL fails, the model sees the error and gets one retry. The corrected SQL is checked again.
- Explains the result in plain English.
- Lets a user upload their own SQLite or CSV file.

## Example questions

- Which employee sold the most last quarter?
- What are the top 3 products by revenue?
- Which category has the most products?

## Safety

1. **Query check:** only a single `SELECT` statement is accepted. Stacked statements and data-changing keywords are rejected.
2. **Read-only database:** the connection is read-only, so even a query that slipped through could not change data.

## Evaluation

The assistant was tested on 20 questions (6 easy, 8 medium, 6 hard), each with a hand-written reference SQL. Results are compared by values, not by SQL text. Score: **20/20**.

The full walkthrough, with every step and result, is in `Text_to_SQL_Assistant.ipynb`.

## Project files

- `Text_to_SQL_Assistant.ipynb`: the full walkthrough in Google Colab. It builds the pipeline step by step (schema reading, prompt, safety checks, self-correction, plain-English answers) and runs the 20-question evaluation.
- `app.py`: the Streamlit web app. It contains the same logic as the notebook, packaged as one file so it can be deployed. This is the file the live demo runs.
- `requirements.txt`: the Python libraries the app needs.
- `README.md`: this page.

## How to run

**Option 1: Google Colab (full walkthrough).** Open the notebook in Colab and add a secret named `GEMINI_API_KEY` (free key from [Google AI Studio](https://aistudio.google.com/apikey)). Steps 0 to 9 need only this key. Step 10 also needs a free ngrok account and an `NGROK_TOKEN` secret.

**Option 2: locally.** Install the libraries, set the key, and start the app.

```
pip install -r requirements.txt
```

Windows (Command Prompt):

```
set GEMINI_API_KEY=your_key_here
streamlit run app.py
```

Mac / Linux:

```
export GEMINI_API_KEY=your_key_here
streamlit run app.py
```

Alternatively, skip the key and paste it into the app's sidebar after starting with `streamlit run app.py`.

## Tech stack

Python, SQLite, pandas, Google Gemini API, Streamlit.

## Limitations

- Tested on one database (Northwind) and 20 questions.
- The scoring does not compare row order.
- The text safety check is strict and also rejects harmless uses of words such as `REPLACE`.
- Gemini model names change over time. If a call fails with "model not found", update `MODEL` in `app.py`.
- The free Gemini plan limits requests per minute.

## Data

Northwind sample database (SQLite version): https://github.com/jpwhite3/northwind-SQLite3

## Author

**Tama Talukder**

LinkedIn: [https://www.linkedin.com/in/tama-talukder/]
