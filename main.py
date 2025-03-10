from sqlalchemy import create_engine
import sqlalchemy
from langchain_community.utilities.sql_database import SQLDatabase
from vertexai import init
from langchain.chat_models import init_chat_model
from langchain_community.agent_toolkits.sql.toolkit import SQLDatabaseToolkit
from langchain.tools import Tool
from langchain import hub
from langchain_core.messages import HumanMessage
from langgraph.prebuilt import create_react_agent
import vertexai
from google.cloud import aiplatform
from langchain_google_vertexai import VertexAI
from langchain.chains import RetrievalQA
from typing import List, TypedDict
from langgraph.graph import StateGraph

from langchain_google_vertexai import (
    VertexAIEmbeddings,
    VectorSearchVectorStore,
)

from prompt import LLM_SQL_SYS_PROMPT
from IPython.display import Image, display


############################################
# 1. 設定資料庫連線
############################################
PROJECT_ID = "tsmccareerhack2025-bsid-grp2"
REGION = "us-central1"  
INSTANCE = "sql-instance-relational"
DATABASE = "postgres" 
TABLE_NAME = "fin_data" 
DB_HOST = "34.56.145.52"  # Cloud SQL Public IP
DB_PORT = "5432"  # PostgreSQL 預設端口
_USER = "postgres"
_PASSWORD = "postgres"

db_url = f'postgresql+psycopg2://{_USER}:{_PASSWORD}@{DB_HOST}:{DB_PORT}/{DATABASE}'
engine = sqlalchemy.create_engine(db_url)
db = SQLDatabase(engine)

############################################
# 2. 初始化 Vertex AI & LLM
############################################
init(project=PROJECT_ID, location=REGION)
llm = init_chat_model("gemini-1.5-pro", model_provider="google_vertexai")

############################################
# 3. 設定 SQL Agent 工具
############################################
toolkit = SQLDatabaseToolkit(db=db, llm=llm)
tools = toolkit.get_tools()
prompt_template = hub.pull("langchain-ai/sql-agent-system-prompt")
system_message = prompt_template.format(dialect="PostgreSQL", top_k=5)
agent_executor = create_react_agent(llm, tools, prompt=system_message)

# 建立 SQL 查詢工具，讓 LLM 生成 SQL
def sql_query_tool(query: str) -> dict:
    """透過 SQL Agent 生成 SQL 並執行，並回傳包含 structured_response 的 dict"""
    # 使用 invoke 並傳入正確格式的輸入（字典格式的 state）
    response = agent_executor.invoke({"messages": [HumanMessage(content=query)]})
    
    # 提取最終答案 (根據實際回傳結構調整)
    if "messages" in response:
        output = response["messages"][-1].content
    else:
        output = str(response)
    
    return {"structured_response": output}



# 創建 Tool 物件
sql_tool = Tool(
    name="sql_db_query",
    func=sql_query_tool,
    description="用來查詢 SQL 數據庫，請輸入財務相關的問題，系統會自動轉換為 SQL 語句並執行。"
)

# 更新 sql_tools，確保包含新的 `sql_tool`
sql_tools = [sql_tool]

############################################
# 4. 設定 RAG 工具
############################################
BUCKET = "tsmccareerhack2025-bsid-grp2-bucket"
BUCKET_URI = f"gs://{BUCKET}"

aiplatform.init(project=PROJECT_ID, location=REGION, staging_bucket=BUCKET_URI)
llm_model = VertexAI(model_name="gemini-1.5-pro")
embedding_model = VertexAIEmbeddings(model_name="text-embedding-005")

# 設定 Vertex AI Vector Search
INDEX_ID = "4438785615936356352"
ENDPOINT_ID = "2966706672111714304"
my_index = aiplatform.MatchingEngineIndex(INDEX_ID)
my_index_endpoint = aiplatform.MatchingEngineIndexEndpoint(ENDPOINT_ID)

# 建立向量資料庫
vector_store = VectorSearchVectorStore.from_components(
    project_id=PROJECT_ID,
    region=REGION,
    gcs_bucket_name=BUCKET,
    index_id=my_index.name,
    endpoint_id=my_index_endpoint.name,
    embedding=embedding_model,
)

# 建立 RetrievalQA Chain
retriever = vector_store.as_retriever()
retriever.search_kwargs = {"k": 10}
retrieval_qa = RetrievalQA.from_chain_type(
    llm=llm,
    chain_type="stuff",
    retriever=retriever,
    return_source_documents=True,
)

# 建立 RAG tool
def query_rag_tool(query: str):
    """使用 Vertex AI Vector Search 進行檢索，並使用 Gemini 1.5 Pro 回答"""
    response = retrieval_qa({"query": query})
    return {"answer": response["result"], "sources": [doc.page_content for doc in response["source_documents"]]}

rag_tool = Tool(
    name="RAG_Search",
    func=query_rag_tool,
    description="Retrieves relevant documents using Vertex AI Vector Search and answers queries."
)
rag_tools = [rag_tool]

############################################
# 5. 建立 LangGraph Agent
############################################
class AgentState(TypedDict):
    query: str
    tools: List[str]
    tool_results: List[str]
    final_answer: str 

def decide_tools(query: str) -> List[str]:
    """
    根據 query，決定要使用哪個 tool。
    如果包含數值型關鍵字 → SQL
    如果包含文本/法說關鍵字 → RAG
    可能同時需要兩者 → 皆用
    如果都沒有 → 回傳空 list
    """
    lower_q = query.lower()

    # 這些是你在圖片裡提到的財報指標，或 fin_data 等關鍵字
    numeric_keywords = [
        "operating income", "cost of goods sold", "operating expense",
        "tax expense", "revenue", "total asset",
        "gross profit margin", "operating margin", "fin_data", "sql", "table"
    ]
    # 這些則是假設要查「法說會議」或其它文字檔用的關鍵字
    text_keywords = ["法說", "會議", "rag", "meeting", "transcript", "txt"]

    need_sql = any(k.lower() in lower_q for k in numeric_keywords)
    need_rag = any(k.lower() in lower_q for k in text_keywords)

    # 依照需求可能同時都要
    if need_sql and need_rag:
        return ["sql_db_query", "RAG_Search"]
    elif need_sql:
        return ["sql_db_query"]
    elif need_rag:
        return ["RAG_Search"]
    else:
        # 都不符合
        return []
    

class Agent:
    def __init__(self, model, sql_tools, rag_tools, role, mode):
        self.model = model
        self.tools = sql_tools + rag_tools  
        self.tool_dict = {t.name: t for t in self.tools}
        self.role = role
        self.mode = mode

        # LangGraph: 建立 StateGraph
        graph = StateGraph(state_schema=AgentState)
        graph.add_node("decide", self.decide_action)
        graph.add_node("adjust_sql_query", self.adjust_sql_query)  # ✅ 新增 SQL 調整 Node
        graph.add_node("action", self.take_action)
        graph.add_node("generate_final_response", self.generate_final_response)  # ✅ 修正名稱避免衝突
        graph.add_node("end", lambda state: state)

        # 設定決策流程
        # graph.add_edge("decide", "action")
        graph.add_conditional_edges(
            "decide",
            lambda state: "adjust_sql_query" if self.is_sql_query(state) else "action"
        )
        graph.add_edge("adjust_sql_query", "action")
        graph.add_edge("action", "generate_final_response")  # ✅ 修正名稱
        graph.add_edge("generate_final_response", "end")

        graph.set_entry_point("decide")
        self.graph = graph.compile()

    def decide_action(self, state: AgentState) -> AgentState:
        """決定應該使用哪些工具"""
        query = state["query"]
        selected_tools = decide_tools(query)
        if not selected_tools:
            return {"query": query, "tools": [], "tool_results": [], "final_answer": "很抱歉，目前沒有對應的資料。"}
        return {"query": query, "tools": selected_tools, "tool_results": [], "final_answer": ""}

    def is_sql_query(self, state: AgentState) -> bool:
        """檢查是否需要 Call SQL tool 調整"""
        return "sql_db_query" in state["tools"]
    
    def adjust_sql_query(self, state: AgentState) -> AgentState:
        """調整 SQL Query，使其更加明確"""
        query = state["query"]
        adjusted_query = self.model.invoke(f"請將以下查詢轉換成具體且明確的 user query: {query}\n" + LLM_SQL_SYS_PROMPT).content
        
        print(f"Adjusted SQL query: {adjusted_query}\n")
        return {"query": adjusted_query, "tools": state["tools"], "tool_results": [], "final_answer": ""}

    def take_action(self, state: AgentState) -> AgentState:
        """執行查詢工具"""
        query = state["query"]
        tool_names = state["tools"]
        results = []
        
        for name in tool_names:
            tool = self.tool_dict.get(name)
            if name in ["sql_db_query"]:
                # user_query = SQL_SYS_PROMPT + query
                tool_result = tool.run(query)
                results.append(f"{name} tool reponse : {tool_result['structured_response']}")
                
            elif name in ["RAG_Search"]:
                tool_result = tool.run(query)
                results.append(f"=== Tool {name} 回傳 ===\n{tool_result}")
            else:
                results.append(f"無法找到對應的 tool: {name}")
                
            print(f"--- {name} tool results:", results)
        return {"query": query, "tools": tool_names, "tool_results": results, "final_answer": ""}

    def generate_final_response(self, state: AgentState) -> AgentState:
        """將 tools 查詢結果與 user 問題整合，再交給 LLM 重新回答"""
        query = state["query"]
        tool_results = "\n".join(state["tool_results"])

        prompt = f"""
        使用者的問題: {query}
        以下是來自不同工具的查詢結果：
        {tool_results}
        ---
        請根據使用者問題的語言回覆, 英文問問題請用英文回答, 以此類推。
        請根據這些資訊，產生一個完整且清楚的回答，並確保你的回答能讓使用者理解。 
        """
        # print('final round query:', prompt)
        
        final_answer = self.model.invoke(prompt)
        return {"query": query, "tools": state["tools"], "tool_results": state["tool_results"], "final_answer": final_answer}

    def run(self, query: str) -> str:
        """對外的介面，餵入 query 後跑 graph，回傳最後 response"""
        init_state: AgentState = {"query": query, "tools": [], "tool_results": [], "final_answer": ""}
        end_state = self.graph.invoke(init_state)
        return end_state["final_answer"]
    

# 新增create_agent函數讓 app.py 可以使用    
def create_agent(role, mode):
    # llm = init_chat_model("gemini-1.5-pro", model_provider="google_vertexai")
    # sql_tools = [sql_tool]
    # rag_tools = [rag_tool]
    #print(role, mode)
    return Agent(model=llm, sql_tools=sql_tools, rag_tools=rag_tools, role=role, mode=mode) 

# 確保 `Agent` 可以被外部匯入
__all__ = ["Agent", "create_agent"]

if __name__ == "__main__":
    # 建立 Agent 物件
    agent = create_agent("GB", "Chat Mode")

    test_queries = [
        "What is Amazon's Revenue in 2022 Q1?",       # show 單一公司單一指標
        "show the `evenue`, `Tax Expense`, `Operating Expense` of Amazon in 2020 Q1.",     # show 單一公司多個指標
        "show the `evenue`, `Taxespense`, `Operating Expense` of Amazon in 2020 Q1.",     # show 單一公司多個指標, 但語法錯誤
        "show the all value of Amazon in 2020 Q1?",     # show 單一公司所有指標
        # "Find Amazon's 2020 Q1 earnings call insights.",
        # "Show both 2020 Amazon Q1 revenue and future outlook."
    ]   
    sigle_value_query = [
        "What is Amazon's Revenue in 2022 Q1?",
        "What is AMD's Operating Income in 2023 Q3?",
    ]
    trend_anlysis_query = [
        "Did Intel's Gross Profit Margin increase in 2020 Q4?",
        "Did Samsung's Operating Expense decrease in 2020 Q3?",
    ]
    
    time_based_comparison_query = [
        "Is Qualcomm's Total Asset in 2023 Q1 higher than in 2022 Q1?",
        "Is TSMC's Operating Margin in 2021 Q2 higher than in 2020 Q2?",
        "Is Microsoft's Tax Expense in 2024 Q1 lower than in 2023 Q4?",
        "Is Google's Revenue in 2022 Q2 higher than in 2021 Q2?",
        "Is Apple's Operating Income in 2021 Q1 higher than in 2020 Q1?",
        "Was Broadcom's Cost of Goods Sold lower in 2020 Q2 compared to Q1?",
    ]
    
    # single value query
    print("Single Value Query:")
    for query in sigle_value_query:
        print("="*50)
        print(f"Query: {query}")
        result = agent.run(query)
        print("Final Response:", result.content)
        
    # trend analysis query
    print("Trend Analysis Query:")
    for query in trend_anlysis_query:
        print("="*50)
        print(f"Query: {query}")
        result = agent.run(query)
        print("Final Response:", result.content)
        
    # time based comparison query
    print("Time Based Comparison Query:")
    for query in time_based_comparison_query:
        print("="*50)
        print(f"Query: {query}")
        result = agent.run(query)
        print("Final Response:", result.content)