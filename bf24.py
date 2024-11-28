import json
import time
import requests
from bs4 import BeautifulSoup
from datetime import datetime
from send import NotificationSender
import os
from pymongo import MongoClient
import cfscrape
import shutil

scraper = cfscrape.create_scraper()  # returns a CloudflareScraper instance

class ForumMonitor:
    def __init__(self, config_path='data/config.json'):
        self.config_path = config_path
        self.proxy_host = os.getenv("PROXY_HOST", None)  # 从环境变量读取代理配置
        self.mongo_host = os.getenv("MONGO_HOST", 'mongodb://localhost:27017/')  # 从环境变量读取代理配置
        self.load_config()

        # 连接到 MongoDB
        self.mongo_client = MongoClient(self.mongo_host)  # 默认连接到本地 MongoDB
        self.db = self.mongo_client['bf24']  # 使用数据库 'forum_monitor'
        self.threads_collection = self.db['threads']  # 线程集合
        self.comments_collection = self.db['comments']  # 评论集合
        try:
            # 创建索引。如果索引已经存在，MongoDB 会自动跳过创建，无需担心重复。
            self.threads_collection.create_index('link', unique=True)
            self.comments_collection.create_index('comment_id', unique=True)
        except Exception as e:
            print(e)


    # 加载配置文件
    def load_config(self):
        try:
            # 检查配置文件是否存在
            if not os.path.exists(self.config_path):
                print(f"{self.config_path} 不存在，复制到 {self.config_path}")
                try: os.mkdir('data')
                except: pass
                shutil.copy('example.json', self.config_path)
            with open(self.config_path, 'r') as f:
                self.config = json.load(f)['config']
                self.notifier = NotificationSender(self.config_path)  # 创建通知发送器
            print("配置文件加载成功")
        except Exception as e:
            print(f"加载配置失败: {e}")
            self.config = {}

    def workers_ai_run(self, model, inputs):
        headers = {"Authorization": f"Bearer {self.config['cf_token']}"}
        input = { "messages": inputs }
        response = requests.post(f"https://api.cloudflare.com/client/v4/accounts/{self.config['cf_account_id']}/ai/run/{model}", headers=headers, json=input)
        return response.json()

    # 用AI总结Thread
    def get_summarize_from_ai(self, description):
        inputs = [
            { "role": "system", "content": self.config['thread_prompt'] }, # "你是一个中文智能助手，帮助我筛选一个 VPS (Virtual Private Server, 虚拟服务器) 交流论坛的信息。接下来我要给你一条信息，请你用50字简短总结，并用100字介绍其提供的价格最低的套餐（介绍其价格、配置以及对应的优惠码，如果有）。格式为：摘要：xxx\n优惠套餐：xxx"
            { "role": "user", "content": description}
        ]

        output = self.workers_ai_run(self.config['model'], inputs) # "@cf/qwen/qwen1.5-14b-chat-awq"
        # print(output)
        return output['result']['response'].split('END')[0]

    # 用AI判断评论是否值得推送
    def get_filter_from_ai(self, description):
        inputs = [
            { "role": "system", "content": self.config['filter_prompt'] }, # "你是一个中文智能助手，帮助我筛选一个 VPS (Virtual Private Server, 虚拟服务器) 交流论坛的信息。接下来我要给你一条信息，如果满足筛选规则，请你返回文段翻译，如果文段超过100字，翻译后再进行摘要，如果不满足，则返回 "FALSE"。 筛选条件：这条评论需要提供了一个新的优惠活动 discount，或是发起了一组抽奖 giveaway，或是提供了优惠码 code，或是补充了供货 restock，除此之外均返回FALSE。返回格式：内容：XXX 或者 FALSE。"
            { "role": "user", "content": description}
        ]

        output = self.workers_ai_run(self.config['model'], inputs) # "@cf/qwen/qwen1.5-14b-chat-awq"
        print(output)
        return output['result']['response'].split('END')[0]



    def handle_thread(self, thread_data):
        # 检查是否已经有该线程
        existing_thread = self.threads_collection.find_one({'link': thread_data['link']})

        if not existing_thread:
            # 存储 RSS 线程到 MongoDB

            self.threads_collection.insert_one(thread_data)  # 仅当线程不存在时插入

            print(f"线程已存储: {thread_data['title']}, 链接: {thread_data['link']}")

            # 解析 pub_date 为 datetime 对象
            time_diff = datetime.utcnow() - thread_data['pub_date']

            # 如果文章发布时间在当前时间的一天内，则发送通知
            if time_diff.total_seconds() <= 24 * 60 * 60:  # 24小时以内
                # 格式化发布时间为所需格式
                formatted_pub_date = thread_data['pub_date'].strftime("%Y/%m/%d %H:%M")
                
                # 生成文章概要
                summary = self.get_summarize_from_ai(thread_data['description'])
                
                # 创建消息内容
                message = (
                    f"{thread_data['cate'].upper()} 新促销\n"
                    f"标题：{thread_data['title']}\n"
                    f"作者：{thread_data['creator']}\n"  # 如果有作者信息，可替换 '未知' 为实际值
                    f"发布时间：{formatted_pub_date}\n\n"
                    f"{thread_data['description'][:200]}...\n\n"
                    f"{summary}\n\n"
                    f"{thread_data['link']}"
                )

                self.notifier.send_message(message)
        else:
            # print(f"线程已存在: {link}")
            pass

    # 获取线程所有页面的评论
    def fetch_comments(self, thread_data):
        thread_info = self.threads_collection.find_one({'link': thread_data['link']})
        if thread_info:
            last_page = thread_info.get('last_page', 1)
        while True:
            # 不同类型可能要考虑不同构建
            if thread_data['cate'] == 'let' or thread_data['cate'] == 'les':
                page_url = f"{thread_data['link']})/p{last_page}"  # 拼接分页 URL

            response = scraper.get(page_url)
            if response.status_code == 200:
                # print(f"抓取页面: {page_url} 成功")
                page_content = response.text
                if thread_data['cate'] == 'let' or thread_data['cate'] == 'les':
                    self.parse_let_les_comment(page_content, thread_data)
                    
                last_page += 1
                time.sleep(2)  # 可以适当延时防止过于频繁的请求
            else:
                # print(f"已获取到最终一页, 共 {last_page-1} 页")
                # 更新 MongoDB 中该线程的 last_page
                self.threads_collection.update_one(
                    {'link': thread_data['link']},
                    {'$set': {'last_page': last_page-1}}
                )
                break  # 如果没有更多页面，则停止抓取

    def handle_comment(self, comment_data, thread_data):
        existing_comment = self.comments_collection.find_one({'comment_id': comment_data['comment_id']})
    
        if not existing_comment:
            # 存储评论到 MongoDB，使用 comment_id 确保唯一性
            self.comments_collection.update_one(
                {'comment_id': comment_data['comment_id']},  # 使用 comment_id 作为唯一标识符
                {'$set': comment_data},
                upsert=True  # 如果该评论不存在则插入，否则更新
            )

            time_diff = datetime.utcnow() - comment_data['created_at']
            # 如果文章发布时间在当前时间的一天内，则发送通知
            if time_diff.total_seconds() <= 24 * 60 * 60:  # 24小时以内
                print(comment_data['message'])
                ai_response = self.get_filter_from_ai(comment_data['message'])
                print(ai_response)
                if not "FALSE" in ai_response:
                    # 格式化发布时间为所需格式
                    formatted_pub_date = comment_data['created_at'].strftime("%Y/%m/%d %H:%M")
    
                    # 创建消息内容
                    message = (
                        f"{thread_data['cate'].upper()} 新评论\n"
                        f"作者：{comment_data['author']}\n"  # 如果有作者信息，可替换 '未知' 为实际值
                        f"发布时间：{formatted_pub_date}\n\n"
                        f"{comment_data['message'][:200]}...\n"
                        f"{ai_response[:200]}...\n\n"
                        f"{comment_data['url']}"
                    )
    
                    self.notifier.send_message(message)
                else:
                    print(f'AI skip {comment_data["message"]}')

    # 检查 RSS
    def check_let(self, url = "https://lowendtalk.com/categories/announcements/feed.rss"):
        print(f"正在检查 LET: {url}")
        response = scraper.get(url)
        if response.status_code == 200:
            rss_feed = response.text
            self.parse_let(rss_feed)
        else:
            print(f"无法获取 LET 数据: {response.status_code}")
 

    # 解析 RSS 内容
    def parse_let(self, rss_feed):
        soup = BeautifulSoup(rss_feed, 'xml')
        items = soup.find_all('item')
        # 只看前 3 个
        for item in items[:3]:
            # print(item)
            title = item.find('title').text
            if not 'black friday' in title.lower():
                continue
            link = item.find('link').text
            description = BeautifulSoup(item.find('description').text,'lxml').text
            pub_date = item.find('pubDate').text
            creator = item.find('dc:creator').text

            thread_data = {
                'cate': 'let',
                'title': title,
                'link': link,
                'description': description,
                'pub_date': datetime.strptime(pub_date, "%a, %d %b %Y %H:%M:%S +0000"),
                'created_at': datetime.utcnow(),
                'creator': creator,
                'last_page': 1  # 默认从第一页开始抓取
            }

            self.handle_thread(thread_data)

            # 开始抓取
            self.fetch_comments(thread_data)
            
    # 解析页面信息
    # 两个论坛的样式是一样的
    def parse_let_les_comment(self, page_content, thread_data):
        soup = BeautifulSoup(page_content, 'html.parser')
        # 获取所有评论
        comments = soup.find_all('li', class_='ItemComment')
        for comment in comments:
            # 检查是否包含指定的 class
            comment_classes = comment.get('class')
            if not comment_classes or not (
                'Role_PatronProvider' in comment_classes or 
                'isOriginalPoster' in comment_classes or 
                'Role_HostingProvider' in comment_classes
            ):
                continue  # 如果不包含目标类，则跳过
            


            # 通过 ID 获取评论唯一标识
            comment_id = comment.get('id')
            if not comment_id:
                print('nocommentid')
                continue  # 如果没有 id，则跳过此评论
            
            comment_id = comment_id.split('_')[1]  # 提取 id 中的数字部分

            # 提取评论中的数据
            author = comment.find('a', class_='Username').text
            message = comment.find('div', class_='Message').text.strip()
            created_at = comment.find('time')['datetime']
            if len(message) <5:
                print('too short')
                continue
            if message.startswith('@'):
                print('start with @')
                continue  # 如果消息以 '@' 开头，则跳过

            comment_data = {
                    'comment_id': f'{thread_data["cate"]}_{comment_id}',  # 使用 comment_id 作为唯一标识符
                    'thread_url': thread_data['link'],
                    'author': author,
                    # 'message': message,
                    # 优化存储
                    'message': message[:200],
                    'created_at': datetime.strptime(created_at, "%Y-%m-%dT%H:%M:%S+00:00"),
                    'created_at_recorded': datetime.utcnow(),
                    'url': f"https://lowendtalk.com/discussion/comment/{comment_id}/#Comment_{comment_id}" if thread_data['cate'] == 'let' else f"https://lowendspirit.com/discussion/comment/{comment_id}/#Comment_{comment_id}"
                }
            # print(comment_data)
            self.handle_comment(comment_data, thread_data)

    def check_les(self,url = 'https://lowendspirit.com/categories/offers'):
        response = requests.get(url)
        print(f"正在检查 LES: {url}")
        if response.status_code == 200:
            text = response.text
            self.parse_les(text)
        else:
            print(f"无法获取 LES 数据: {response.status_code}")

    def parse_les(self,text):
        soup = BeautifulSoup(text, 'html.parser')
        items = soup.find_all('li',class_='ItemDiscussion')

        for item in items[:3]:
            title = item.find('div', {'class': 'Title'}).find('a').text.strip()
            creator = item.find('span', {'class': 'DiscussionAuthor'}).find('a').text.strip()
            link = item.find('div', {'class': 'Title'}).find('a')['href']

            response = requests.get(link)

            soup = BeautifulSoup(response.text, 'html.parser')
            discussion_div = soup.find('div', {'class': 'Discussion'})
            content = discussion_div.find('div', {'class': 'Message userContent'}).text.strip()
            publish_time = discussion_div.find('span', {'class': 'MItem DateCreated'}).find('time')['datetime']

            thread_data = {
                'cate': 'les',
                'title': title,
                'creator': creator,
                'link': link,
                'description': content,
                'pub_date': datetime.strptime(publish_time, '%Y-%m-%dT%H:%M:%S%z').replace(tzinfo=None),
                'create_at': datetime.utcnow(),
                'last_page': 1
            }

            self.handle_thread(thread_data)

            # 开始抓取
            self.fetch_comments(thread_data)

    # 监控主循环
    def start_monitoring(self):
        print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} 开始监控...")
        frequency = self.config.get('frequency', 600)  # 默认每10分钟检测一次
        
        debug = True

        while True:
            if debug:
                    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} 开始遍历...")
                    self.check_let()  # 检查 RSS
                    # self.check_les()
                    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} 遍历完成...")
            else:
                try:
                    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} 开始遍历...")
                    self.check_let()  # 检查 RSS
                    # self.check_les()
                    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} 遍历完成...")
                except Exception as e:
                    print(f"检测过程出现错误: {e}")
            time.sleep(frequency)

    # 外部重载配置方法
    def reload(self):
        print("重新加载配置...")
        self.load_config()

# 示例运行
if __name__ == "__main__":
    monitor = ForumMonitor(config_path='data/config.json')
    monitor.start_monitoring()
