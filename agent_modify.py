from langgraph.graph import StateGraph
from typing import List, TypedDict
import json
from langchain_core.messages import HumanMessage
from sql_search import get_sql_tools
from rag_search import get_rag_tools
from tools import decide_tools, fix_rag_result
from langchain_google_vertexai import VertexAI
from langgraph.checkpoint.memory import MemorySaver
from langchain.chat_models import init_chat_model
from prompt import LLM_SQL_SYS_PROMPT, USER_DECIDE_SEARCH_PROMPT, MULTI_RAG_PROMPT, FINAL_GENERATE_PROMPT, FIRST_ASKED_PROMPT, INVALID_QUERY_PROMPT
from config import (PROJECT_ID, REGION, BUCKET, INDEX_ID, 
                    ENDPOINT_ID, BUCKET_URI, 
                    MODEL_NAME, EMBEDDING_MODEL_NAME, MODEL_PROVIDER
                    )

def decide_tools(llm, query: str):
    """根據 Query 選擇 SQL 或 RAG 工具"""
    lower_q = query.lower()

    prompt = USER_DECIDE_SEARCH_PROMPT.format(query=lower_q)

    result = llm.invoke(prompt).content
    start = result.find("{")
    end = result.rfind("}") + 1
    json_part = result[start:end]

    # 手動解析
    search_types = eval(json_part.split('"search_types":')[1].split("]")[0].strip().strip(":").strip() + "]")
    print("Search Types:", search_types)

    return search_types


class AgentState(TypedDict):
    query: str
    adjusted_query: str
    tools: List[str]
    tool_results: List[str]
    final_answer: str  # 這裡的 key 改為 final_answer 避免衝突


class Agent:
    def __init__(self, model, sql_tools, rag_tools, role):
        self.role = role
        self.model = model
        self.is_first = True
        self.is_FISCAL = None
        self.is_USD = None
        self.is_end = False
        self.tools = sql_tools + rag_tools  
        self.tool_dict = {t.name: t for t in self.tools}

        # LangGraph: 建立 StateGraph
        graph = StateGraph(state_schema=AgentState)
        graph.add_node("decide", self.start_chat)
        graph.add_node("adjust_sql_query", self.adjust_sql_query)  # ✅ 新增 SQL 調整 Node
        graph.add_node("adjust_rag_query", self.adjust_rag_query)  # ✅ 新增 SQL 調整 Node
        graph.add_node("sql_action", self.take_action_sql)
        graph.add_node("rag_action", self.take_action_rag)
        graph.add_node("action", self.take_action)
        graph.add_node("generate_final_response", self.generate_final_response)  # ✅ 修正名稱避免衝突
        graph.add_node("end", lambda state: state)

        # 設定決策流程
        graph.add_conditional_edges(
            "decide",
            lambda state: "end" if self.is_end
                    else "adjust_sql_query" if (self.is_sql_query(state) or self.is_rag_query(state)) 
                    else "action"
        )
        graph.add_edge("adjust_sql_query", "sql_action")
        graph.add_edge("sql_action", "adjust_rag_query")
        graph.add_edge("adjust_rag_query", "rag_action")
        graph.add_edge("rag_action", "generate_final_response")
        graph.add_edge("action", "end")
        graph.add_edge("generate_final_response", "end")

        graph.set_entry_point("decide")
        memory = MemorySaver()
        self.graph = graph.compile(checkpointer=memory)

    def start_chat(self, state: AgentState) -> AgentState:
        """決定應該使用哪些工具"""
        if not self.is_first:
            query = state["query"] + "\n" +  state["adjusted_query"]   ## 幫確認
        else:
            query = state["query"]
        prompt = FIRST_ASKED_PROMPT.format(query=query)
        result = llm.invoke(prompt).content
        print(result)
        data = {}
        json_str = result.replace('```json', '').replace('```', '').strip()
        # 分行處理字串
        for line in json_str.splitlines():
            # 跳過不包含冒號的行
            if ':' not in line:
                continue
            # 分割鍵和值
            key, value = line.split(':', 1)
            # 移除多餘的符號和空白
            key = key.strip().strip('"')
            value = value.strip().strip('",[] ')
            # 將值存入字典
            data[key] = value
        if not (data["fiscal"] and data["USD"]):
            self.is_end = True
            if not (data["fiscal"] or data["USD"]):
                final_answer = "請問您的年度是使用財年或者歷年，以及希望呈現的幣值(USD/TWD)?"
            elif not data["fiscal"]:
                final_answer = "請問您的年度是使用財年或者歷年?"
            elif not data["USD"]:
                final_answer = "希望呈現的幣值(USD/TWD)?"
            state["final_answer"] = final_answer

            state["adjusted_query"] = query

            self.is_first = False

            return state
        self.is_FISCAL = data["fiscal"]
        self.is_USD = data["USD"]
        state["tools"] = data["tools"]
        state["query"] = query
        self.is_end = False
        self.is_first = False
        
        
        return state
    
    def is_sql_query(self, state: AgentState) -> bool:
        """檢查是否需要 Call SQL tool 調整"""
        return "sql_db_query" in state["tools"]
    
    def is_rag_query(self, state: AgentState) -> bool:
        """檢查是否需要 Call SQL tool 調整"""
        return "RAG_Search" in state["tools"]

    def adjust_sql_query(self, state: AgentState) -> AgentState:
        """調整 SQL Query，使其更加明確"""
        if not self.is_sql_query(state):
            return state
        
        query = state["query"]
        prompt = LLM_SQL_SYS_PROMPT.format(query=query)
        adjusted_query = self.model.invoke(prompt).content
        print(f"Adjusted SQL query: {adjusted_query}\n")
        return {"query": query, "adjusted_query": adjusted_query, "tools": state["tools"], "tool_results": [], "final_answer": ""}
 

    def adjust_rag_query(self, state: AgentState) -> AgentState:
        """調整 RAG Query，使其更加明確"""
        if not self.is_rag_query(state):
            return state
        
        query = state["query"]
        prompt = MULTI_RAG_PROMPT.format(query=query)
        is_multi_rag = self.model.invoke(prompt)
        multi_rag = fix_rag_result(is_multi_rag.content)

        adjusted_query = ""
        for each_query in multi_rag["Split Queries"]:

            adjusted_query += each_query+'\n'
        
        print(f"Adjusted SQL query: {adjusted_query}\n")
        state["adjusted_query"] = adjusted_query
        return state
        # return {"query": query, "adjusted_query": adjusted_query, "tools": state["tools"], "tool_results": [], "final_answer": ""}

    def take_action_sql(self, state: AgentState) -> AgentState:
        """執行查詢工具"""
        if not self.is_sql_query(state):
            return state
        
        query = state["adjusted_query"]
        tool_names = state["tools"]
        results = state["tool_results"]
        
        name = "sql_db_query"
        tool = self.tool_dict.get(name)
            # user_query = SQL_SYS_PROMPT + query
        tool_result = tool.run(query)
        results.append(f"{name} tool reponse : {tool_result['structured_response']}")
            
        print(f"--- {name} tool results:", results)
        state["tool_results"] = results
        return state
        # return {"query": state["query"], "adjusted_query": query, "tools": tool_names, "tool_results": results, "final_answer": ""}
    
    def take_action_rag(self, state: AgentState) -> AgentState:
        """執行查詢工具"""
        if not self.is_rag_query(state):
            return state
        
        queries = state["adjusted_query"]
        results = state["tool_results"]
        name = "RAG_Search"
        tool = self.tool_dict.get(name)

        for query in queries.split('\n'):
            
            # user_query = SQL_SYS_PROMPT + query
            tool_result = tool.run(query)
            results.append(f"{name} tool reponse : {tool_result['structured_response']}")
        
        # llm
        print(f"--- {name} tool results:", results)
        state["tool_results"] = results
        return state
        # return {"query": state["query"], "adjusted_query": queries, "tools": tool_names, "tool_results": results, "final_answer": ""}
    
    def take_action(self, state: AgentState) -> AgentState:
        """執行查詢工具"""
        query = state["query"]
        prompt = INVALID_QUERY_PROMPT.format(query=query)

        
        result = llm.invoke(prompt).content
            
        print("--- action results:", result)
        state["final_answer"] = result
        return state
        # return {"query": query, "adjusted_query": state["adjusted_query"], "tools": state["tools"], "tool_results": state["tool_results"], "final_answer": result}

    def generate_final_response(self, state: AgentState) -> AgentState:
        """將 tools 查詢結果與 user 問題整合，再交給 LLM 重新回答"""
        query = state["query"]
        tool_results = "\n".join(state["tool_results"])

        prompt = FINAL_GENERATE_PROMPT.format(query=query, tool_results=tool_results)
        # print('final round query:', prompt)
        
        final_answer = self.model.invoke(prompt)
        state["final_answer"] = final_answer.content
        return state
        # return {"query": query, "tools": state["tools"], "tool_results": state["tool_results"], "final_answer": final_answer}

    def run(self, query: str, state: AgentState):
        """對外的介面，餵入 query 後跑 graph，回傳最後 response"""
        if state:
            state: AgentState = {"query": query, "tools": [], "tool_results": [], "final_answer": ""}
        state["query"] = query
        end_state = self.graph.invoke(state, config={"configurable": {"thread_id": "unique_thread_id"}})

        return end_state["final_answer"], end_state

llm = init_chat_model(MODEL_NAME, model_provider=MODEL_PROVIDER)

sql_tool = get_sql_tools()
rag_tool = get_rag_tools()
print("SQL Tools:", sql_tool, "\nRAG Tools:", rag_tool)
agent = Agent(model=llm, sql_tools=sql_tool, rag_tools=rag_tool, role="user")

if __name__ == "__main__":
    # 建立 Agent 物件
    # llm = init_chat_model(MODEL_NAME, model_provider=MODEL_PROVIDER)
    # agent = Agent(model=llm, sql_tools=get_sql_tools(), rag_tools=get_rag_tools())

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
    state = {"query": "",
    "adjusted_query": "",
    "tools": [],
    "tool_results": [],
    "final_answer":  ""}
    for query in sigle_value_query:
        print("="*50)
        print(f"Query: {query}")
        result = agent.run(query, state)
        print("Final Response:", result)
        
    # # trend analysis query
    # print("Trend Analysis Query:")
    # for query in trend_anlysis_query:
    #     print("="*50)
    #     print(f"Query: {query}")
    #     result = agent.run(query)
    #     print("Final Response:", result.content)
        
    # # time based comparison query
    # print("Time Based Comparison Query:")
    # for query in time_based_comparison_query:
    #     print("="*50)
    #     print(f"Query: {query}")
    #     result = agent.run(query)
    #     print("Final Response:", result.content)
    
    print("Done!")