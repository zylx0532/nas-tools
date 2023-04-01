import re
import xml.dom.minidom
import traceback
from threading import Lock

import log
from app.downloader import Downloader
from app.filter import Filter
from app.helper import DbHelper
from app.media import Media
from app.media.meta import MetaInfo
from app.sites import Sites, SiteConf
from app.subscribe import Subscribe
from app.utils import DomUtils, RequestUtils, StringUtils, ExceptionUtils, RssTitleUtils, Torrent
from app.utils.types import MediaType, SearchType
from config import Config

lock = Lock()


class Rss:
    _sites = []
    filter = None
    media = None
    sites = None
    siteconf = None
    downloader = None
    searcher = None
    dbhelper = None
    subscribe = None

    def __init__(self):
        self.init_config()

    def init_config(self):
        self.media = Media()
        self.downloader = Downloader()
        self.sites = Sites()
        self.siteconf = SiteConf()
        self.filter = Filter()
        self.dbhelper = DbHelper()
        self.subscribe = Subscribe()
        self._sites = self.sites.get_sites(rss=True)

    def rssdownload(self):
        """
        RSS订阅检索下载入口，由定时服务调用
        """
        rss_items = {}

        def __update_no_exist(rss_id, download_item, match_info, no_exists):
            rss_items.setdefault(rss_id, {})
            rss_items[rss_id].setdefault('media_list', []).append(download_item)
            rss_items[rss_id]['match_info'] = match_info
            rss_items[rss_id]['no_exists'] = no_exists
        if not self._sites:
            return

        with lock:
            log.info("【Rss】开始RSS订阅...")

            # 读取电影订阅
            rss_movies = self.subscribe.get_subscribe_movies(state='R')
            if not rss_movies:
                log.warn("【Rss】没有正在订阅的电影")
            else:
                log.info("【Rss】电影订阅清单：%s"
                         % " ".join('%s' % info.get("name") for _, info in rss_movies.items()))
            # 读取电视剧订阅
            rss_tvs = self.subscribe.get_subscribe_tvs(state='R')
            if not rss_tvs:
                log.warn("【Rss】没有正在订阅的电视剧")
            else:
                log.info("【Rss】电视剧订阅清单：%s"
                         % " ".join('%s' % info.get("name") for _, info in rss_tvs.items()))
            # 没有订阅退出
            if not rss_movies and not rss_tvs:
                return

            # 获取有订阅的站点范围
            check_sites = []
            check_all = False
            for rid, rinfo in rss_movies.items():
                rss_sites = rinfo.get("rss_sites")
                if not rss_sites:
                    check_all = True
                    break
                else:
                    check_sites += rss_sites
            if not check_all:
                for rid, rinfo in rss_tvs.items():
                    rss_sites = rinfo.get("rss_sites")
                    if not rss_sites:
                        check_all = True
                        break
                    else:
                        check_sites += rss_sites
            if check_all:
                check_sites = []
            else:
                check_sites = list(set(check_sites))

            total_num = 0
            # 遍历站点资源
            for site_info in self._sites:
                if not site_info:
                    continue
                # 站点名称
                site_name = site_info.get("name")
                # 没有订阅的站点中的不检索
                if check_sites and site_name not in check_sites:
                    continue
                # 站点rss链接
                rss_url = site_info.get("rssurl")
                if not rss_url:
                    log.info(f"【Rss】{site_name} 未配置rssurl，跳过...")
                    continue
                site_cookie = site_info.get("cookie")
                site_ua = site_info.get("ua")
                # 是否解析种子详情
                site_parse = site_info.get("parse")
                # 是否使用代理
                site_proxy = site_info.get("proxy")
                # 使用的规则
                site_fliter_rule = site_info.get("rule")
                # 开始下载RSS
                log.info(f"【Rss】正在处理：{site_name}")
                if site_info.get("pri"):
                    site_order = 100 - int(site_info.get("pri"))
                else:
                    site_order = 0
                rss_acticles = self.parse_rssxml(rss_url)
                if not rss_acticles:
                    log.warn(f"【Rss】{site_name} 未下载到数据")
                    continue
                else:
                    log.info(f"【Rss】{site_name} 获取数据：{len(rss_acticles)}")
                # 处理RSS结果
                res_num = 0
                for article in rss_acticles:
                    try:
                        # 种子名
                        title = article.get('title')
                        # 种子链接
                        enclosure = article.get('enclosure')
                        # 种子页面
                        page_url = article.get('link')
                        # 种子大小
                        size = article.get('size')
                        # 开始处理
                        log.info(f"【Rss】开始处理：{title}")
                        # 检查这个种子是不是下过了
                        if self.dbhelper.is_torrent_rssd(enclosure):
                            log.info(f"【Rss】{title} 已成功订阅过")
                            continue
                        # 识别种子名称，开始检索TMDB
                        media_info = MetaInfo(title=title)
                        cache_info = self.media.get_cache_info(media_info)
                        if cache_info.get("id"):
                            # 使用缓存信息
                            media_info.tmdb_id = cache_info.get("id")
                            media_info.type = cache_info.get("type")
                            media_info.title = cache_info.get("title")
                            media_info.year = cache_info.get("year")
                        else:
                            # 重新查询TMDB
                            media_info = self.media.get_media_info(title=title)
                            if not media_info:
                                log.warn(f"【Rss】{title} 无法识别出媒体信息！")
                                continue
                            elif not media_info.tmdb_info:
                                log.info(f"【Rss】{title} 识别为 {media_info.get_name()} 未匹配到TMDB媒体信息")
                        # 大小及种子页面
                        media_info.set_torrent_info(size=size,
                                                    page_url=page_url,
                                                    site=site_name,
                                                    site_order=site_order,
                                                    enclosure=enclosure)
                        # 检查种子是否匹配订阅，返回匹配到的订阅ID、是否洗版、总集数、上传因子、下载因子
                        match_flag, match_msg, match_info = self.check_torrent_rss(
                            media_info=media_info,
                            rss_movies=rss_movies,
                            rss_tvs=rss_tvs,
                            site_filter_rule=site_fliter_rule,
                            site_cookie=site_cookie,
                            site_parse=site_parse,
                            site_ua=site_ua,
                            site_proxy=site_proxy)
                        for msg in match_msg:
                            log.info(f"【Rss】{msg}")

                        # 未匹配
                        if not match_flag:
                            continue

                        # 非模糊匹配命中，检查本地情况，检查删除订阅
                        if not match_info.get("fuzzy_match"):
                            # 匹配到订阅，如没有TMDB信息则重新查询
                            if not media_info.tmdb_info and media_info.tmdb_id:
                                media_info.set_tmdb_info(self.media.get_tmdb_info(mtype=media_info.type,
                                                                                  tmdbid=media_info.tmdb_id))
                            if not media_info.tmdb_info:
                                continue
                            over_edition = match_info.get("over_edition")
                            exist_flag, no_exists = self.subscribe.get_no_exists(media_info, match_info, over_edition)
                            if not over_edition and exist_flag:
                                # 本地已存在
                                continue
                        else:
                            # 不做处理，直接下载
                            pass

                        # 设置种子信息
                        media_info.set_torrent_info(res_order=match_info.get("res_order"),
                                                    filter_rule=match_info.get("filter_rule"),
                                                    over_edition=match_info.get("over_edition"),
                                                    download_volume_factor=match_info.get("download_volume_factor"),
                                                    upload_volume_factor=match_info.get("upload_volume_factor"),
                                                    rssid=match_info.get("id"))
                        # 设置下载参数
                        media_info.set_download_info(download_setting=match_info.get("download_setting"),
                                                     save_path=match_info.get("save_path"))
                        # 插入数据库历史记录
                        self.dbhelper.insert_rss_torrents(media_info)
                        # 加入下载列表

                        __update_no_exist(match_info.get("id"), media_info, match_info, no_exists)
                        res_num = res_num + 1
                        total_num += 1
                    except Exception as e:
                        log.error("【Rss】处理RSS发生错误：" + "".join(traceback.format_exception(e)))
                        continue
                log.info("【Rss】%s 处理结束，匹配到 %s 个有效资源" % (site_name, res_num))
            log.info("【Rss】所有RSS处理结束，共 %s 个有效资源" % total_num)
            if not rss_items:
                return
            for rid, item in rss_items.items():
                self.subscribe.subscribe_media(item['match_info'], item['media_list'], item['no_exists'])

    @staticmethod
    def parse_rssxml(url, proxy=False):
        """
        解析RSS订阅URL，获取RSS中的种子信息
        :param url: RSS地址
        :return: 种子信息列表
        """
        _special_title_sites = {
            'pt.keepfrds.com': RssTitleUtils.keepfriends_title
        }

        # 开始处理
        ret_array = []
        if not url:
            return []
        site_domain = StringUtils.get_url_domain(url)
        try:
            ret = RequestUtils(proxies=Config().get_proxies() if proxy else None).get_res(url)
            if not ret:
                return []
            ret.encoding = ret.apparent_encoding
        except Exception as e2:
            ExceptionUtils.exception_traceback(e2)
            log.console(str(e2))
            return []
        if ret:
            ret_xml = ret.text
            try:
                # 解析XML
                dom_tree = xml.dom.minidom.parseString(ret_xml)
                rootNode = dom_tree.documentElement
                items = rootNode.getElementsByTagName("item")
                for item in items:
                    try:
                        # 标题
                        title = DomUtils.tag_value(item, "title", default="")
                        if not title:
                            continue
                        # 标题特殊处理
                        if site_domain and site_domain in _special_title_sites:
                            title = _special_title_sites.get(site_domain)(title)
                        # 描述
                        description = DomUtils.tag_value(item, "description", default="")
                        # 种子页面
                        link = DomUtils.tag_value(item, "link", default="")
                        # 种子链接
                        enclosure = DomUtils.tag_value(item, "enclosure", "url", default="")
                        if not enclosure and not link:
                            continue
                        # 部分RSS只有link没有enclosure
                        if not enclosure and link:
                            enclosure = link
                            link = None
                        # 大小
                        size = DomUtils.tag_value(item, "enclosure", "length", default=0)
                        if size and str(size).isdigit():
                            size = int(size)
                        else:
                            size = 0
                        # 发布日期
                        pubdate = DomUtils.tag_value(item, "pubDate", default="")
                        if pubdate:
                            # 转换为时间
                            pubdate = StringUtils.get_time_stamp(pubdate)
                        # 返回对象
                        tmp_dict = {'title': title,
                                    'enclosure': enclosure,
                                    'size': size,
                                    'description': description,
                                    'link': link,
                                    'pubdate': pubdate}
                        ret_array.append(tmp_dict)
                    except Exception as e1:
                        ExceptionUtils.exception_traceback(e1)
                        continue
            except Exception as e2:
                ExceptionUtils.exception_traceback(e2)
                return ret_array
        return ret_array

    def check_torrent_rss(self,
                          media_info,
                          rss_movies,
                          rss_tvs,
                          site_filter_rule,
                          site_cookie,
                          site_parse,
                          site_ua,
                          site_proxy):
        """
        判断种子是否命中订阅
        :param media_info: 已识别的种子媒体信息
        :param rss_movies: 电影订阅清单
        :param rss_tvs: 电视剧订阅清单
        :param site_filter_rule: 站点过滤规则
        :param site_cookie: 站点的Cookie
        :param site_parse: 是否解析种子详情
        :param site_ua: 站点请求UA
        :param site_proxy: 是否使用代理
        :return: 匹配到的订阅ID、是否洗版、总集数、匹配规则的资源顺序、上传因子、下载因子，匹配的季（电视剧）
        """
        # 默认值
        # 匹配状态 0不在订阅范围内 -1不符合过滤条件 1匹配
        match_flag = False
        # 匹配的rss信息
        match_msg = []
        match_rss_info = {}
        # 上传因素
        upload_volume_factor = None
        # 下载因素
        download_volume_factor = None
        hit_and_run = False

        # 匹配电影
        if media_info.type == MediaType.MOVIE and rss_movies:
            for rid, rss_info in rss_movies.items():
                rss_sites = rss_info.get('rss_sites')
                # 过滤订阅站点
                if rss_sites and media_info.site not in rss_sites:
                    continue
                # tmdbid或名称年份匹配
                name = rss_info.get('name')
                year = rss_info.get('year')
                tmdbid = rss_info.get('tmdbid')
                fuzzy_match = rss_info.get('fuzzy_match')
                # 非模糊匹配
                if not fuzzy_match:
                    # 有tmdbid时使用tmdbid匹配
                    if tmdbid and not tmdbid.startswith("DB:"):
                        if str(media_info.tmdb_id) != str(tmdbid):
                            continue
                    else:
                        # 豆瓣年份与tmdb取向不同
                        if year and str(media_info.year) not in [str(year),
                                                                 str(int(year) + 1),
                                                                 str(int(year) - 1)]:
                            continue
                        if name != media_info.title:
                            continue
                # 模糊匹配
                else:
                    # 匹配年份
                    if year and str(year) != str(media_info.year):
                        continue
                    # 匹配关键字或正则表达式
                    search_title = f"{media_info.org_string} {media_info.title} {media_info.year}"
                    if not re.search(name, search_title, re.I) and name not in search_title:
                        continue
                # 媒体匹配成功
                match_flag = True
                match_rss_info = rss_info
                break
        # 匹配电视剧
        elif rss_tvs:
            # 匹配种子标题
            for rid, rss_info in rss_tvs.items():
                rss_sites = rss_info.get('rss_sites')
                # 过滤订阅站点
                if rss_sites and media_info.site not in rss_sites:
                    continue
                # 有tmdbid时精确匹配
                name = rss_info.get('name')
                year = rss_info.get('year')
                season = rss_info.get('season')
                tmdbid = rss_info.get('tmdbid')
                fuzzy_match = rss_info.get('fuzzy_match')
                # 非模糊匹配
                if not fuzzy_match:
                    if tmdbid and not tmdbid.startswith("DB:"):
                        if str(media_info.tmdb_id) != str(tmdbid):
                            continue
                    else:
                        # 匹配年份，年份可以为空
                        if year and str(year) != str(media_info.year):
                            continue
                        # 匹配名称
                        if name != media_info.title:
                            continue
                    # 匹配季，季可以为空
                    if season and season != media_info.get_season_string():
                        continue
                # 模糊匹配
                else:
                    # 匹配季，季可以为空
                    if season and season != "S00" and season != media_info.get_season_string():
                        continue
                    # 匹配年份
                    if year and str(year) != str(media_info.year):
                        continue
                    # 匹配关键字或正则表达式
                    search_title = f"{media_info.org_string} {media_info.title} {media_info.year}"
                    if not re.search(name, search_title, re.I) and name not in search_title:
                        continue
                # 媒体匹配成功
                match_flag = True
                match_rss_info = rss_info
                break
        # 名称匹配成功，开始过滤
        if match_flag:
            # 解析种子详情
            if site_parse:
                # 检测Free
                torrent_attr = self.siteconf.check_torrent_attr(torrent_url=media_info.page_url,
                                                                cookie=site_cookie,
                                                                ua=site_ua,
                                                                proxy=site_proxy)
                if torrent_attr.get('2xfree'):
                    download_volume_factor = 0.0
                    upload_volume_factor = 2.0
                elif torrent_attr.get('free'):
                    download_volume_factor = 0.0
                    upload_volume_factor = 1.0
                else:
                    upload_volume_factor = 1.0
                    download_volume_factor = 1.0
                if torrent_attr.get('hr'):
                    hit_and_run = True
                # 设置属性
                media_info.set_torrent_info(upload_volume_factor=upload_volume_factor,
                                            download_volume_factor=download_volume_factor,
                                            hit_and_run=hit_and_run)
            # 订阅无过滤规则应用站点设置
            filter_rule = match_rss_info.get('filter_rule') or site_filter_rule
            filter_dict = {
                "restype": match_rss_info.get('filter_restype'),
                "pix": match_rss_info.get('filter_pix'),
                "team": match_rss_info.get('filter_team'),
                "rule": filter_rule
            }
            match_filter_flag, res_order, match_filter_msg = self.filter.check_torrent_filter(meta_info=media_info,
                                                                                              filter_args=filter_dict)
            if not match_filter_flag:
                match_msg.append(match_filter_msg)
                return False, match_msg, match_rss_info
            else:
                match_msg.append("%s 识别为 %s %s 匹配订阅成功" % (
                    media_info.org_string,
                    media_info.get_title_string(),
                    media_info.get_season_episode_string()))
                match_msg.append(f"种子描述：{media_info.subtitle}")
                match_rss_info.update({
                    "res_order": res_order,
                    "filter_rule": filter_rule,
                    "upload_volume_factor": upload_volume_factor,
                    "download_volume_factor": download_volume_factor})
                return True, match_msg, match_rss_info
        else:
            match_msg.append("%s 识别为 %s %s 不在订阅范围" % (
                media_info.org_string,
                media_info.get_title_string(),
                media_info.get_season_episode_string()))
            return False, match_msg, match_rss_info
