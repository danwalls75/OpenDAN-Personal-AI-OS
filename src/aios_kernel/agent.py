from typing import Optional

from asyncio import Queue
import asyncio
import logging
import uuid
import time
import json
import shlex
import datetime
import copy

from .agent_base import AgentMsg, AgentMsgStatus, AgentMsgType,FunctionItem,LLMResult,AgentPrompt,AgentReport,AgentTodo,AgentGoal,AgentTodoResult,AgentWorkLog
from .chatsession import AIChatSession
from .compute_task import ComputeTaskResult,ComputeTaskResultCode
from .ai_function import AIFunction
from .environment import Environment
from .contact_manager import ContactManager,Contact,FamilyMember
from .compute_kernel import ComputeKernel
from .bus import AIBus
from .workspace_env import WorkspaceEnvironment

from knowledge import *

logger = logging.getLogger(__name__)


DEFAULT_AGENT_READ_REPORT_PROMPT = """
"""

DEFAULT_AGENT_DO_PROMPT = """
You are a helpful AI assistant.
Solve tasks using your coding and language skills.
In the following cases, suggest python code (in a python coding block) for the user to execute.
    1. When you need to collect info, use the code to output the info you need, for example, browse or search the web, download/read a file, print the content of a webpage or a file, get the current date/time, check the operating system. After sufficient info is printed and the task is ready to be solved based on your language skill, you can solve the task by yourself.
    2. When you need to perform some task with code, use the code to perform the task and output the result. Finish the task smartly.
Solve the task step by step if you need to. If a plan is not provided, explain your plan first. Be clear which step uses code, and which step uses your language skill.
When using code, you must indicate the script type in the code block. The user cannot provide any other feedback or perform any other action beyond executing the code you suggest. The user can't modify your code. So do not suggest incomplete code which requires users to modify. Don't use a code block if it's not intended to be executed by the user.
If you want the user to save the code in a file before executing it, put # filename: <filename> inside the code block as the first line. Don't include multiple code blocks in one response. Do not ask users to copy and paste the result. Instead, use 'print' function for the output when relevant. Check the execution result returned by the user.
If the result indicates there is an error, fix the error and output the code again. Suggest the full code instead of partial code or code changes. If the error can't be fixed or if the task is not solved even after the code is executed successfully, analyze the problem, revisit your assumption, collect additional info you need, and think of a different approach to try.
When you find an answer, verify the answer carefully. Include verifiable evidence in your response if possible.
Reply "TERMINATE" in the end when everything is done.
"""

DEFAULT_AGENT_SELF_CHECK_PROMPT = """

"""

DEFAULT_AGENT_GOAL_TO_TODO_PROMPT = """
我会给你一个目标，你需要结合自己的角色思考如何将其拆解成多个TODO。请直接返回json来表达这些TODO
"""

DEFAULT_AGENT_LEARN_PROMPT = """
你拥有非常优秀的资料整理技能。我给你一段内容，你会尝试对其进行摘要，并在已有的资料库中找到合适的位置存放该文章。
1. 结合你的角色和组织的工作目标构建摘要，尽量精简，长度不要超过256个字
2. 资料库以文件系统的形式组织，浏览知识库是成本高昂的操作，应尝试从根目录往子目录深入来找到最合适的信息。必要的情况下，你可以在合适的位置创建新的目录。为了方便浏览，每一层目录的文件夹数不超过32个，名称长度不超过16个字符，目录深度不超过6层
3. 你可以从不同的角度给出最多3个合适的位置
4. 返回一个json来保存摘要和建议保存位置信息
"""

DEFAULT_AGENT_LEARN_LONG_CONENT_PROMPT = """
我给你一段内容，尝试为期建立目录。目录的标题不能超过16个字，
目录要指向正文的位置（用字符偏移即可），整个目录的文本长度不能超过256个字节。并用json表达这个目录
"""
class AIAgentTemplete:
    def __init__(self) -> None:
        self.llm_model_name:str = "gpt-4-0613"
        self.max_token_size:int = 0
        self.template_id:str = None
        self.introduce:str = None
        self.author:str = None
        self.prompt:AgentPrompt = None

    def load_from_config(self,config:dict) -> bool:
        if config.get("llm_model_name") is not None:
            self.llm_model_name = config["llm_model_name"]
        if config.get("max_token_size") is not None:
            self.max_token_size = config["max_token_size"]
        if config.get("template_id") is not None:
            self.template_id = config["template_id"]
        if config.get("prompt") is not None:
            self.prompt = AgentPrompt()
            if self.prompt.load_from_config(config["prompt"]) is False:
                logger.error("load prompt from config failed!")
                return False


        return True


class AIAgent:
    def __init__(self) -> None:
        self.role_prompt:AgentPrompt = None
        self.agent_prompt:AgentPrompt = None
        self.agent_think_prompt:AgentPrompt = None
        self.llm_model_name:str = None
        self.max_token_size:int = 3600
        self.agent_energy = 15
        self.agent_task = None
        self.last_recover_time = time.time()
        

        self.agent_id:str = None
        self.template_id:str = None
        self.fullname:str = None
        self.powerby = None
        self.enable = True
        self.enable_kb = False
        self.enable_timestamp = False
        self.guest_prompt_str = None 
        self.owner_promp_str = None
        self.contact_prompt_str = None
        self.history_len = 10

        self.review_todo_prompt = None

        self.read_report_prompt = AgentPrompt(DEFAULT_AGENT_READ_REPORT_PROMPT)

        self.do_prompt = AgentPrompt(DEFAULT_AGENT_DO_PROMPT)
        self.self_check_prompt = AgentPrompt(DEFAULT_AGENT_SELF_CHECK_PROMPT)

        self.goal_to_todo_prompt = AgentPrompt(DEFAULT_AGENT_GOAL_TO_TODO_PROMPT)

        self.learn_token_limit = 500
        self.learn_prompt = AgentPrompt(DEFAULT_AGENT_LEARN_PROMPT)

        self.chat_db = None
        self.unread_msg = Queue() # msg from other agent
        self.owner_env : Environment = None
        self.owenr_bus = None
        self.enable_function_list = None

    @classmethod
    def create_from_templete(cls,templete:AIAgentTemplete, fullname:str):
        # Agent just inherit from templete on craete,if template changed,agent will not change
        result_agent = AIAgent()
        result_agent.llm_model_name = templete.llm_model_name
        result_agent.max_token_size = templete.max_token_size
        result_agent.template_id = templete.template_id
        result_agent.agent_id = "agent#" + uuid.uuid4().hex
        result_agent.fullname = fullname
        result_agent.powerby = templete.author
        result_agent.agent_prompt = templete.prompt
        return result_agent

    def load_from_config(self,config:dict) -> bool:
        if config.get("instance_id") is None:
            logger.error("agent instance_id is None!")
            return False
        self.agent_id = config["instance_id"]

        if config.get("fullname") is None:
            logger.error(f"agent {self.agent_id} fullname is None!")
            return False
        self.fullname = config["fullname"]

        if config.get("prompt") is not None:
            self.agent_prompt = AgentPrompt()
            self.agent_prompt.load_from_config(config["prompt"])
        
        if config.get("think_prompt") is not None:
            self.agent_think_prompt = AgentPrompt()
            self.agent_think_prompt.load_from_config(config["think_prompt"])

        if config.get("guest_prompt") is not None:
            self.guest_prompt_str = config["guest_prompt"]

        if config.get("owner_prompt") is not None:
            self.owner_promp_str = config["owner_prompt"]
        
        if config.get("contact_prompt") is not None:
            self.contact_prompt_str = config["contact_prompt"]

        if config.get("owner_env") is not None:
            self.owner_env = config.get("owner_env")
       

        if config.get("powerby") is not None:
            self.powerby = config["powerby"]
        if config.get("template_id") is not None:
            self.template_id = config["template_id"]
        if config.get("llm_model_name") is not None:
            self.llm_model_name = config["llm_model_name"]
        if config.get("max_token_size") is not None:
            self.max_token_size = config["max_token_size"]
        if config.get("enable_function") is not None:
            self.enable_function_list = config["enable_function"]
        if config.get("enable_kb") is not None:
            self.enable_kb = bool(config["enable_kb"])
        if config.get("enable_timestamp") is not None:
            self.enable_timestamp = bool(config["enable_timestamp"])
        if config.get("history_len"):
            self.history_len = int(config.get("history_len"))
        return True
    
    def get_id(self) -> str:
        return self.agent_id

    def get_fullname(self) -> str:
        return self.fullname

    def get_template_id(self) -> str:
        return self.template_id

    def get_llm_model_name(self) -> str:
        return self.llm_model_name

    def get_max_token_size(self) -> int:
        return self.max_token_size
    
    def get_llm_learn_token_limit(self) -> int:
        return self.learn_token_limit
    
    def get_learn_prompt(self) -> AgentPrompt:
        return self.learn_prompt
    
    def get_agent_role_prompt(self) -> AgentPrompt:
        return self.role_prompt

    
    def _get_remote_user_prompt(self,remote_user:str) -> AgentPrompt:
        cm = ContactManager.get_instance()
        contact = cm.find_contact_by_name(remote_user)
        if contact is None:
            #create guest prompt
            if self.guest_prompt_str is not None:
                prompt = AgentPrompt()
                prompt.system_message = {"role":"system","content":self.guest_prompt_str}
                return prompt
            return None
        else:
            if contact.is_family_member:
                if self.owner_promp_str is not None:
                    real_str = self.owner_promp_str.format_map(contact.to_dict())
                    prompt = AgentPrompt()
                    prompt.system_message = {"role":"system","content":real_str}
                    return prompt
            else:
                if self.contact_prompt_str is not None:
                    real_str = self.contact_prompt_str.format_map(contact.to_dict())
                    prompt = AgentPrompt()
                    prompt.system_message = {"role":"system","content":real_str}
                    return prompt
                
        return None

    def _get_inner_functions(self) -> dict:
        if self.owner_env is None:
            return None,0

        all_inner_function = self.owner_env.get_all_ai_functions()
        if all_inner_function is None:
            return None,0

        result_func = []
        result_len = 0
        for inner_func in all_inner_function:
            func_name = inner_func.get_name()
            if self.enable_function_list is not None:
                if len(self.enable_function_list) > 0:
                    if func_name not in self.enable_function_list:
                        logger.debug(f"ageint {self.agent_id} ignore inner func:{func_name}")
                        continue

            this_func = {}
            this_func["name"] = func_name
            this_func["description"] = inner_func.get_description()
            this_func["parameters"] = inner_func.get_parameters()
            result_len += len(json.dumps(this_func)) / 4
            result_func.append(this_func)

        return result_func,result_len

    async def _execute_func(self,inner_func_call_node:dict,prompt:AgentPrompt,inner_functions,org_msg:AgentMsg=None,stack_limit = 5) -> ComputeTaskResult:
        func_name = inner_func_call_node.get("name")
        arguments = json.loads(inner_func_call_node.get("arguments"))
        logger.info(f"llm execute inner func:{func_name} ({json.dumps(arguments)})")

        func_node : AIFunction = self.owner_env.get_ai_function(func_name)
        if func_node is None:
            result_str = f"execute {func_name} error,function not found"
        else:
            if org_msg:
                ineternal_call_record = AgentMsg.create_internal_call_msg(func_name,arguments,org_msg.get_msg_id(),org_msg.target)

            try:
                result_str:str = await func_node.execute(**arguments)
            except Exception as e:
                result_str = f"execute {func_name} error:{str(e)}"
                logger.error(f"llm execute inner func:{func_name} error:{e}")


        logger.info("llm execute inner func result:" + result_str)
        
        prompt.messages.append({"role":"function","content":result_str,"name":func_name})
        task_result:ComputeTaskResult = await ComputeKernel.get_instance().do_llm_completion(prompt,self.llm_model_name,self.max_token_size,inner_functions)
        if task_result.result_code != ComputeTaskResultCode.OK:
            logger.error(f"llm compute error:{task_result.error_str}")
            return task_result
        
        ineternal_call_record.result_str = task_result.result_str
        ineternal_call_record.done_time = time.time()
        if org_msg:
            org_msg.inner_call_chain.append(ineternal_call_record)

        inner_func_call_node = None
        if stack_limit > 0:
            result_message : dict = task_result.result.get("message")
            if result_message:
                inner_func_call_node = result_message.get("function_call")

        if inner_func_call_node:
            return await self._execute_func(inner_func_call_node,prompt,org_msg,stack_limit-1)
        else:
            return task_result
        
    def get_agent_prompt(self) -> AgentPrompt:
        return self.agent_prompt
    
    async def _get_agent_think_prompt(self) -> AgentPrompt:
        return self.agent_think_prompt

    def _format_msg_by_env_value(self,prompt:AgentPrompt):
        if self.owner_env is None:
            return

        for msg in prompt.messages:
            old_content = msg.get("content")
            msg["content"] = old_content.format_map(self.owner_env)

    async def _handle_event(self,event):
        if event.type == "AgentThink":
            return await self.do_self_think()
        



    # async def _process_group_chat_msg(self,msg:AgentMsg) -> AgentMsg:  
    #     session_topic = msg.target + "#" + msg.topic
    #     chatsession = AIChatSession.get_session(self.agent_id,session_topic,self.chat_db)
    #     workspace = self.get_current_workspace()
    #     need_process = False
    #     if msg.mentions is not None:
    #         if self.agent_id in msg.mentions:
    #             need_process = True
    #             logger.info(f"agent {self.agent_id} recv a group chat message from {msg.sender},but is not mentioned,ignore!")

    #     if need_process is not True:
    #         chatsession.append(msg)
    #         resp_msg = msg.create_group_resp_msg(self.agent_id,"")
    #         return resp_msg
    #     else:
    #         msg_prompt = AgentPrompt()
    #         msg_prompt.messages = [{"role":"user","content":f"{msg.sender}:{msg.body}"}]

    #         prompt = AgentPrompt()
    #         prompt.append(self.get_agent_prompt())

    #         if workspace:
    #             prompt.append(workspace.get_prompt())
    #             prompt.append(workspace.get_role_prompt(self.agent_id))

    #         if self.need_session_summmary(msg,chatsession):
    #             # get relate session(todos) summary
    #             summary = self.llm_select_session_summary(msg,chatsession)
    #             prompt.append(AgentPrompt(summary))

    #         self._format_msg_by_env_value(prompt)
    #         inner_functions,function_token_len = self._get_inner_functions()
       
    #         system_prompt_len = prompt.get_prompt_token_len()
    #         input_len = len(msg.body)

    #         history_prmpt,history_token_len = await self._get_prompt_from_session_for_groupchat(chatsession,system_prompt_len + function_token_len,input_len)
    #         prompt.append(history_prmpt) # chat context
    #         prompt.append(msg_prompt)

    #         logger.debug(f"Agent {self.agent_id} do llm token static system:{system_prompt_len},function:{function_token_len},history:{history_token_len},input:{input_len}, totoal prompt:{system_prompt_len + function_token_len + history_token_len} ")
    #         task_result = await self._do_llm_complection(prompt,inner_functions,msg)
    #         if task_result.result_code != ComputeTaskResultCode.OK:
    #             error_resp = msg.create_error_resp(task_result.error_str)
    #             return error_resp
            
    #         final_result = task_result.result_str
    #         llm_result : LLMResult = LLMResult.from_str(final_result)
    #         is_ignore = False
    #         result_prompt_str = ""
    #         match llm_result.state:
    #             case "ignore":
    #                 is_ignore = True
    #             case "waiting":
    #                 for sendmsg in llm_result.send_msgs:
    #                     target = sendmsg.target
    #                     sendmsg.sender = self.agent_id
    #                     sendmsg.topic = msg.topic
    #                     sendmsg.prev_msg_id = msg.get_msg_id()
    #                     send_resp = await AIBus.get_default_bus().send_message(sendmsg)
    #                     if send_resp is not None:
    #                         result_prompt_str += f"\n{target} response is :{send_resp.body}"
    #                         agent_sesion = AIChatSession.get_session(self.agent_id,f"{sendmsg.target}#{sendmsg.topic}",self.chat_db)
    #                         agent_sesion.append(sendmsg)
    #                         agent_sesion.append(send_resp)

    #                 final_result = llm_result.resp + result_prompt_str

    #         if is_ignore is not True:
    #             resp_msg = msg.create_group_resp_msg(self.agent_id,final_result)
    #             chatsession.append(msg)
    #             chatsession.append(resp_msg)

    #             return resp_msg

    #         return None
    def get_workspace_by_msg(self,msg:AgentMsg) -> WorkspaceEnvironment:
        return None
    
    def need_session_summmary(self,msg:AgentMsg,session:AIChatSession) -> bool:
        return False

    async def _process_msg(self,msg:AgentMsg,workspace = None) -> AgentMsg:
        msg_prompt = AgentPrompt()
        if msg.msg_type == AgentMsgType.TYPE_GROUPMSG:
            need_process = False
            msg_prompt.messages = [{"role":"user","content":f"{msg.sender}:{msg.body}"}]
            session_topic = msg.target + "#" + msg.topic
            chatsession = AIChatSession.get_session(self.agent_id,session_topic,self.chat_db)

            if msg.mentions is not None:
                if self.agent_id in msg.mentions:
                    need_process = True
                    logger.info(f"agent {self.agent_id} recv a group chat message from {msg.sender},but is not mentioned,ignore!")
            
            if need_process is not True:
                chatsession.append(msg)
                resp_msg = msg.create_group_resp_msg(self.agent_id,"")
                return resp_msg
        else:
            msg_prompt.messages = [{"role":"user","content":msg.body}]
            session_topic = msg.get_sender() + "#" + msg.topic
            chatsession = AIChatSession.get_session(self.agent_id,session_topic,self.chat_db)

        workspace = self.get_workspace_by_msg(msg)

        prompt = AgentPrompt()
        if workspace:
            prompt.append(workspace.get_prompt())
            prompt.append(workspace.get_role_prompt(self.agent_id))

        prompt.append(self.get_agent_prompt())
        prompt.append(self._get_remote_user_prompt(msg.sender))
        self._format_msg_by_env_value(prompt)
    
        if self.need_session_summmary(msg,chatsession):
            # get relate session(todos) summary
            summary = self.llm_select_session_summary(msg,chatsession)
            prompt.append(AgentPrompt(summary))
        
        inner_functions,function_token_len = self._get_inner_functions()
        system_prompt_len = prompt.get_prompt_token_len()
        input_len = len(msg.body)
        if msg.msg_type == AgentMsgType.TYPE_GROUPMSG:
            history_prmpt,history_token_len = await self._get_prompt_from_session_for_groupchat(chatsession,system_prompt_len + function_token_len,input_len)
        else:
            history_prmpt,history_token_len = await self.get_prompt_from_session(chatsession,system_prompt_len + function_token_len,input_len)      
        prompt.append(history_prmpt) # chat context

        prompt.append(msg_prompt)

        
        logger.debug(f"Agent {self.agent_id} do llm token static system:{system_prompt_len},function:{function_token_len},history:{history_token_len},input:{input_len}, totoal prompt:{system_prompt_len + function_token_len + history_token_len} ")
        #task_result:ComputeTaskResult = await ComputeKernel.get_instance().do_llm_completion(prompt,self.llm_model_name,self.max_token_size,inner_functions)
        task_result = await self._do_llm_complection(prompt,inner_functions,msg)
        if task_result.result_code != ComputeTaskResultCode.OK:
            error_resp = msg.create_error_resp(task_result.error_str)
            return error_resp
        
        final_result = task_result.result_str

        llm_result : LLMResult = LLMResult.from_str(final_result)
        
        # extra_info include the operation about workspace
        if llm_result.extra_info is not None:
            await workspace.update_state_by_msg(msg,llm_result.extra_info)

        is_ignore = False
        result_prompt_str = ""
        match llm_result.state:
            case "ignore":
                is_ignore = True
            case "waiting": # like inner call
                for sendmsg in llm_result.send_msgs:
                    sendmsg.sender = self.agent_id
                    target = sendmsg.target
                    sendmsg.topic = msg.topic
                    sendmsg.prev_msg_id = msg.get_msg_id()
                    send_resp = await AIBus.get_default_bus().send_message(sendmsg)
                    if send_resp is not None:
                        result_prompt_str += f"\n{target} response is :{send_resp.body}"
                        agent_sesion = AIChatSession.get_session(self.agent_id,f"{sendmsg.target}#{sendmsg.topic}",self.chat_db)
                        agent_sesion.append(sendmsg)
                        agent_sesion.append(send_resp)

                final_result = llm_result.resp + result_prompt_str

        if is_ignore is not True:
            if msg.msg_type == AgentMsgType.TYPE_GROUPMSG:
                resp_msg = msg.create_group_resp_msg(self.agent_id,final_result)
            else:
                resp_msg = msg.create_resp_msg(final_result)
            chatsession.append(msg)
            chatsession.append(resp_msg)

            return resp_msg

        return None


    
    async def _get_history_prompt_for_think(self,chatsession:AIChatSession,summary:str,system_token_len:int,pos:int)->(AgentPrompt,int):
        history_len = (self.max_token_size * 0.7) - system_token_len
        
        messages = chatsession.read_history(self.history_len,pos,"natural") # read
        result_token_len = 0
        result_prompt = AgentPrompt()
        have_summary = False
        if summary is not None:
            if len(summary) > 1:
                have_summary = True
        
        if have_summary:
                result_prompt.messages.append({"role":"user","content":summary})
                result_token_len -= len(summary)
        else:
            result_prompt.messages.append({"role":"user","content":"There is no summary yet."})
            result_token_len -= 6

        read_history_msg = 0
        history_str : str = ""
        for msg in messages:
            read_history_msg += 1
            dt = datetime.datetime.fromtimestamp(float(msg.create_time))
            formatted_time = dt.strftime('%y-%m-%d %H:%M:%S')
            record_str = f"{msg.sender},[{formatted_time}]\n{msg.body}\n"
            history_str = history_str + record_str

            history_len -= len(msg.body)
            result_token_len += len(msg.body)
            if history_len < 0:
                logger.warning(f"_get_prompt_from_session reach limit of token,just read {read_history_msg} history message.")
                break
        
        result_prompt.messages.append({"role":"user","content":history_str})
        return result_prompt,pos+read_history_msg
    
    async def _get_prompt_from_session_for_groupchat(self,chatsession:AIChatSession,system_token_len,input_token_len,is_groupchat=False):
        history_len = (self.max_token_size * 0.7) - system_token_len - input_token_len
        messages = chatsession.read_history(self.history_len) # read
        result_token_len = 0
        result_prompt = AgentPrompt()
        read_history_msg = 0
        for msg in reversed(messages):
            read_history_msg += 1
            dt = datetime.datetime.fromtimestamp(float(msg.create_time))
            formatted_time = dt.strftime('%y-%m-%d %H:%M:%S')

            if msg.sender == self.agent_id:
                if self.enable_timestamp:
                    result_prompt.messages.append({"role":"assistant","content":f"(create on {formatted_time}) {msg.body} "})
                else:
                    result_prompt.messages.append({"role":"assistant","content":msg.body})
                
            else:
                if self.enable_timestamp:
                    result_prompt.messages.append({"role":"user","content":f"(create on {formatted_time}) {msg.body} "})
                else:
                    result_prompt.messages.append({"role":"user","content":f"{msg.sender}:{msg.body}"})

            history_len -= len(msg.body)
            result_token_len += len(msg.body)
            if history_len < 0:
                logger.warning(f"_get_prompt_from_session reach limit of token,just read {read_history_msg} history message.")
                break

        return result_prompt,result_token_len



    async def _llm_summary_work(self,workspace:WorkspaceEnvironment):
        # read report ,and update work summary of 
        # build todo list from work summary and goals
        # 
        report_list = self.get_unread_reports()
        
        for report in report_list:
            if self.agent_energy <= 0:
                break
            # merge report to work summary
            await self._llm_read_report(report,workspace)
            self.agent_energy -= 1

        if workspace.is_mgr(self.agent_id):
            # manager can do more work
            await self._llm_review_team(workspace)
            self.agent_energy -= 5
            await self._llm_review_unassigned_todos(workspace)
            self.agent_energy -= 5


    async def _llm_review_team(self,workspace:WorkspaceEnvironment):
        pass

    async def _llm_review_unassigned_todos(self,workspace:WorkspaceEnvironment):
        pass
 
    async def _llm_read_report(self,report:AgentReport,worksapce:WorkspaceEnvironment):
        work_summary = worksapce.get_work_summary(self.agent_id)
        prompt : AgentPrompt = AgentPrompt()
        prompt.append(self.agent_prompt)
        prompt.append(worksapce.get_role_prompt(self.agent_id))
        prompt.append(self.read_report_prompt)
        # report is a message from other agent(human) about work
        prompt.append(AgentPrompt(work_summary))
        prompt.append(AgentPrompt(report.content))

        task_result:ComputeTaskResult = await self._do_llm_complection(prompt)

        if task_result.error_str is not None:
            logger.error(f"_llm_read_report compute error:{task_result.error_str}")
            return

        worksapce.set_work_summary(self.agent_id,task_result.result_str)

        
    # 尝试完成自己的TOOD （不依赖任何其他Agnet）
    async def do_my_work(self) -> None:
        workspace = self.get_current_workspace()

        # review todo能更整体的思考一次todo的优先级
        if await self.need_review_todos():
            await self._llm_review_todos(workspace)

        todo_list = workspace.get_todo_list(self.agent_id)
        
        for todo in todo_list:
            if self.agent_energy <= 0:
                break
            
            if await self.can_do(todo,workspace) is False:
                continue

            if todo.try_count() < 2:
                need_think_todo_from_goal = False
                do_result : AgentTodoResult = await self._llm_do(todo,workspace) 
                self.agent_energy -= 1
                if do_result.result_state == "done":
                    await self._llm_check_todo(todo,workspace)
                    self.agent_energy -= 1

    def get_review_todo_prompt(self) -> AgentPrompt:
        return self.review_todo_prompt

    async def need_review_todos(self) -> bool:
        if self.get_review_todo_prompt() is None:
            return False
        return True

    async def _llm_review_todos(self,workspace:WorkspaceEnvironment):
        prompt = AgentPrompt()

        prompt.append(workspace.get_prompt())
        prompt.append(workspace.get_role_prompt(self.agent_id))
        prompt.append(self.get_review_todo_prompt())

        todo_tree = workspace.get_todo_tree("/")
        prompt.append(AgentPrompt(todo_tree))
        inner_functions,function_token_len = self._get_inner_functions()

        task_result:ComputeTaskResult = await self._do_llm_complection(prompt,inner_functions)
        if task_result.result_code != ComputeTaskResultCode.OK:
            logger.error(f"_llm_review_todos compute error:{task_result.error_str}")
            return
        
        return 
    
    def get_do_prompt(self,todo_type:str) -> AgentPrompt:
        return self.do_prompt

    async def can_do(self,todo:AgentTodo,workspace:WorkspaceEnvironment) -> bool:
        return True

    async def _llm_do(self,todo:AgentTodo,workspace:WorkspaceEnvironment) -> AgentTodoResult:
        prompt : AgentPrompt = AgentPrompt()
        prompt.append(self.agent_prompt)
        prompt.append(workspace.get_role_prompt(self.agent_id))
        
        do_prompt = workspace.get_do_prompt(todo.type)
        if do_prompt is None:
            do_prompt = self.get_do_prompt(todo.type)

        prompt.append(do_prompt)

        # 有通用的todo执行方法，也有定制的，针对特定类型TODO更高效的执行方法
        # 根据经验，Agent可以自主掌握/整理更多类型的TODO的执行方法

        #prompt.append(do_log_prompt)
        prompt.append(self.get_prompt_from_todo(todo))

        task_result:ComputeTaskResult = await self._do_llm_complection(prompt,workspace.get_inner_functions(todo.type))
        
        if task_result.error_str is not None:
            logger.error(f"_llm_do compute error:{task_result.error_str}")
            
        llm_result = LLMResult.from_str(task_result.result_str)
        todo.append_do_result(self.agent_id,llm_result)



        return task_result

    async def _llm_check_todo(self, todo:AgentTodo,workspace:WorkspaceEnvironment) -> bool:
        if self.get_check_prompt(todo) is None:
            return True
        
        prompt : AgentPrompt = AgentPrompt()
        prompt.append(self.agent_prompt)
        prompt.append(workspace.get_role_prompt(self.agent_id))
        prompt.append(self.get_check_prompt(todo))
        if todo.last_check_result:
            prompt.append(AgentPrompt(todo.last_check_result))

        prompt.append(todo.detail)
        prompt.append(todo.result)

        task_result:ComputeTaskResult = await self._do_llm_complection(prompt,workspace.get_inner_functions())

        if task_result.result_code != ComputeTaskResultCode.OK:
            logger.error(f"_llm_check_todo compute error:{task_result.error_str}")
            return False

        if task_result.result_str == "OK":
            return True
        todo.last_check_result = task_result.result_str
        return False
        
    # 尝试自我学习，会主动获取、读取资料并进行整理
    # LLM的本质能力是处理海量知识，应该让LLM能基于知识把自己的工作处理的更好
    def do_self_learn(self) -> None:
        # 不同的workspace是否应该有不同的学习方法？ 
        learn_power = self.get_learn_power()
        kb = self.get_knowledge_base()
        for item in kb.un_learn_items():
            if learn_power <= 0:
                break
            match item.type():
                case "book":
                    self.llm_read_book(kb,item)
                    learn_power -= 1
                case "article":
                    # 可以用vdb 对不同目录的名字进行选择后，先进行一次快速的插入。有时间再慢慢用LLM整理
                    self.llm_read_article(kb,item)
                    learn_power -= 1
                case "video":
                    self.llm_watch_video(kb,item)
                    learn_power -= 1
                case "audio":
                    self.llm_listen_audio(kb,item)
                    learn_power -= 1
                case "code_project":
                    self.llm_read_code_project(kb,item)
                    learn_power -= 1
                case "image":
                    self.llm_view_image(kb,item)
                    learn_power -= 1
                case "other":
                    self.llm_read_other(kb,item)
                    learn_power -= 1
                case _:
                    self.llm_learn_any(kb,item)
                    pass
        
        # 整理自己的知识库(让分类更平衡，更由于自己以后的工作)，并尝试更新学习目标
        current_path = "/"
        current_list = kb.get_list(current_path)
        self_assessment_with_goal = self.get_self_assessment_with_goal()
        learn_goal = {}
        
        
        llm_blance_knowledge_base(current_path,current_list,self_assessment_with_goal,learn_goal,learn_power)

        # 主动学习
        # 方法目前只有使用搜索引擎一种？
        for goal in learn_goal.items():
            self.llm_learn_with_search_engine(kb,goal,learn_power)
            if learn_power <= 0:
                break
           

    def parser_learn_llm_result(self,llm_result:LLMResult):
        pass

    async def _llm_read_article(self,item:KnowledgeObject) -> ComputeTaskResult:
        full_content = item.get_article_full_content()
        full_content_len = ComputeKernel.llm_num_tokens_from_text(full_content,self.get_llm_model_name())
        if full_content_len < self.get_llm_learn_token_limit():
            
            # 短文章不用总结catelog
            #path_list,summary = llm_get_summary(summary,full_content)
            prompt = self.get_agent_role_prompt()
            learn_prompt = self.get_learn_prompt()
            cotent_prompt = AgentPrompt(full_content)
            prompt.append(learn_prompt)
            prompt.append(cotent_prompt)
            
            env_functions = self._get_inner_functions()
            
            task_result:ComputeTaskResult = await self._do_llm_complection(prompt,env_functions)
            if task_result.result_code != ComputeTaskResultCode.OK:
                return task_result
            llm_result = LLMResult.from_str(task_result.result_str)
            path_list,summary = self.parser_learn_llm_result(llm_result)

        else:
            # 用传统方法对文章进行一些处理，目的是尽可能减少LLM调用的次数
            catelog = item.get_articl_catelog()
            chunk_content = full_content.read(self.get_llm_learn_token_limit())
            summary = kb.try_get_summary(catelog,full_content)
        
            while chunk_content is not None:
                #path_list,summarycatelog = llm_get_summary(summary,chunk_content)
                #learn_prompt = self.get_learn_prompt_with_summary()

                prompt = AgentPrompt("summary")
                learn_prompt.append(prompt)
                prompt = AgentPrompt(chunk_content)
                learn_prompt.append(prompt)
                
                #llm_result = self.do_llm_competion(learn_prompt)
                #path_list,summary,catelog = parser_learn_llm_result(llm_result)

                #chunk_content = full_content.read(self.get_llm_learn_token_limit())
            
        kb.insert_item(path_list,item,catelog,summary) 

    async def do_self_think(self):
        session_id_list = AIChatSession.list_session(self.agent_id,self.chat_db)
        for session_id in session_id_list:
            if self.agent_energy <= 0:
                break
            used_energy = await self.think_chatsession(session_id)
            self.agent_energy -= used_energy

        todo_logs = await self.get_todo_logs()
        for todo_log in todo_logs:
            if self.agent_energy <= 0:
                break
            used_energy = await self.think_todo_log(todo_log)
            self.agent_energy -= used_energy

        return 
    

    async def think_todo_log(self,todo_log:AgentWorkLog):
        pass

    async def think_chatsession(self,session_id):
        if self.agent_think_prompt is None:
            return
        logger.info(f"agent {self.agent_id} think session {session_id}")
        chatsession = AIChatSession.get_session_by_id(session_id,self.chat_db)

        while True:
            cur_pos = chatsession.summarize_pos
            summary = chatsession.summary
            prompt:AgentPrompt = AgentPrompt()
            #prompt.append(self._get_agent_prompt())
            prompt.append(await self._get_agent_think_prompt())
            system_prompt_len = prompt.get_prompt_token_len()
            #think env?
            history_prompt,next_pos = await self._get_history_prompt_for_think(chatsession,summary,system_prompt_len,cur_pos)
            prompt.append(history_prompt)
            is_finish = next_pos - cur_pos < 2
            if is_finish:
                logger.info(f"agent {self.agent_id} think session {session_id} is finished!,no more history")
                break
            #3) llm summarize chat history
            task_result:ComputeTaskResult = await ComputeKernel.get_instance().do_llm_completion(prompt,self.llm_model_name,self.max_token_size,None)
            if task_result.result_code != ComputeTaskResultCode.OK:
                logger.error(f"llm compute error:{task_result.error_str}")
                break
            else:
                new_summary= task_result.result_str
                logger.info(f"agent {self.agent_id} think session {session_id} from {cur_pos} to {next_pos} summary:{new_summary}")
                chatsession.update_think_progress(next_pos,new_summary)    
        return 
    
    async def get_prompt_from_session(self,chatsession:AIChatSession,system_token_len,input_token_len) -> AgentPrompt:
        # TODO: get prompt from group chat is different from single chat
        
        history_len = (self.max_token_size * 0.7) - system_token_len - input_token_len
        messages = chatsession.read_history(self.history_len) # read
        result_token_len = 0
        result_prompt = AgentPrompt()
        read_history_msg = 0

        if chatsession.summary is not None:
            if len(chatsession.summary) > 1:  
                result_prompt.messages.append({"role":"user","content":chatsession.summary})
                result_token_len -= len(chatsession.summary)

        for msg in reversed(messages):
            read_history_msg += 1
            dt = datetime.datetime.fromtimestamp(float(msg.create_time))
            formatted_time = dt.strftime('%y-%m-%d %H:%M:%S')

            if msg.sender == self.agent_id:
                if self.enable_timestamp:
                    result_prompt.messages.append({"role":"assistant","content":f"(create on {formatted_time}) {msg.body} "})
                else:
                    result_prompt.messages.append({"role":"assistant","content":msg.body})
                
            else:
                if self.enable_timestamp:
                    result_prompt.messages.append({"role":"user","content":f"(create on {formatted_time}) {msg.body} "})
                else:
                    result_prompt.messages.append({"role":"user","content":msg.body})

            history_len -= len(msg.body)
            result_token_len += len(msg.body)
            if history_len < 0:
                logger.warning(f"_get_prompt_from_session reach limit of token,just read {read_history_msg} history message.")
                break

        return result_prompt,result_token_len
    
    async def _do_llm_complection(self,prompt:AgentPrompt,inner_functions:dict=None,org_msg:AgentMsg=None) -> ComputeTaskResult:
        from .compute_kernel import ComputeKernel
        #logger.debug(f"Agent {self.agent_id} do llm token static system:{system_prompt_len},function:{function_token_len},history:{history_token_len},input:{input_len}, totoal prompt:{system_prompt_len + function_token_len + history_token_len} ")
        task_result:ComputeTaskResult = await ComputeKernel.get_instance().do_llm_completion(prompt,self.llm_model_name,self.max_token_size,inner_functions)
        if task_result.result_code != ComputeTaskResultCode.OK:
            logger.error(f"llm compute error:{task_result.error_str}")
            #error_resp = msg.create_error_resp(task_result.error_str)
            return task_result

        result_message = task_result.result.get("message")
        inner_func_call_node = None
        if result_message:
            inner_func_call_node = result_message.get("function_call")

        if inner_func_call_node:
            call_prompt : AgentPrompt = copy.deepcopy(prompt)
            task_result = await self._execute_func(inner_func_call_node,call_prompt,inner_functions,org_msg)
            
        return task_result
    
    def need_work(self) -> bool:
        return True
    
    def need_self_think(self) -> bool:
        return True
    
    def need_self_learn(self) -> bool:
        return True
    
    def wake_up(self) -> None:
        if self.agent_task is None:
            self.agent_task = asyncio.create_task(self._on_timer)
        else:
            logger.warning(f"agent {self.agent_id} is already wake up!")

    # agent loop
    async def _on_timer(self):
        while True:
            await asyncio.sleep(1)
            now = time.time()
            if now - self.last_recover_time > 60:
                self.agent_energy += (now - self.last_recover_time) / 60
                self.last_recover_time = now
            else:
                return

            # complete todo
            if self.need_work():
                await self.do_my_work()

            # review other's todo
            # self.review_other_works()

            # do work summary
            if self.need_self_think():
                await self.do_self_think()

            # 
            if self.need_self_learn():
                await self.do_self_learn()

        
     
        
    
