
def decide_tools(query: str):
    """根據 Query 選擇 SQL 或 RAG 工具"""
    lower_q = query.lower()

    numeric_keywords = ["revenue", "operating income", "sql", "fin_data"]
    text_keywords = ["法說", "meeting", "transcript", "rag"]

    need_sql = any(k in lower_q for k in numeric_keywords)
    need_rag = any(k in lower_q for k in text_keywords)


    
    if need_sql and need_rag:
        return ["sql_db_query", "RAG_Search"]
    elif need_sql:
        return ["sql_db_query"]
    elif need_rag:
        return ["RAG_Search"]
    return []

def fix_rag_result(text):
            
        text = text.replace("`", '"').replace("json", "")
        company_name_start = text.find('"Company Name": [') + len('"Company Name": [')
        company_name_end = text.find("]", company_name_start)
        company_names = text[company_name_start:company_name_end].replace('"', '').split(", ")

        calendar_year_start = text.find('"CALENDAR_YEAR": [') + len('"CALENDAR_YEAR": [')
        calendar_year_end = text.find("]", calendar_year_start)
        calendar_years = text[calendar_year_start:calendar_year_end].replace('"', '').split(", ")

        calendar_qtr_start = text.find('"CALENDAR_QTR": [') + len('"CALENDAR_QTR": [')
        calendar_qtr_end = text.find("]", calendar_qtr_start)
        calendar_qtrs = text[calendar_qtr_start:calendar_qtr_end].replace('"', '').split(", ")

        # ✅ 取得 Multiple Values Exist
        multiple_values_start = text.find('"Multiple Values Exist": "') + len('"Multiple Values Exist": "')
        multiple_values_end = text.find('"', multiple_values_start)
        multiple_values_exist = text[multiple_values_start:multiple_values_end]

        # ✅ 取得 Split Queries
        queries_start = text.find('"Split Queries": [') + len('"Split Queries": [')
        queries_end = text.find("]", queries_start)
        queries = text[queries_start:queries_end].replace('"', '').split(",\n        ")

        print(" - Company Name:", company_names)
        print(" - CALENDAR_YEAR:", calendar_years)
        print(" - CALENDAR_QTR:", calendar_qtrs)
        print("\nMultiple Values Exist:", multiple_values_exist)
        print("\nSplit Queries:")
        for q in queries:
            print(" -", q.strip())
        return {"Company Name": company_names, "CALENDAR_YEAR": calendar_years, "CALENDAR_QTR": calendar_qtrs, "Multiple Values Exist": multiple_values_exist, "Split Queries": queries}