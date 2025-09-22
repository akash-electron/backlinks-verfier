from fastapi import FastAPI, HTTPException, BackgroundTasks, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, validator
from typing import List, Optional, Dict, Any, Iterator
import requests
from bs4 import BeautifulSoup
import pandas as pd
from urllib.parse import urlparse
import time
import random
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from concurrent.futures import ThreadPoolExecutor, as_completed
import logging
import uuid
from datetime import datetime
import json
import io
import os

# Selenium imports for JavaScript content
# from selenium import webdriver
# from selenium.webdriver.chrome.options import Options
# from selenium.webdriver.common.by import By
# from selenium.webdriver.support.ui import WebDriverWait
# from selenium.webdriver.support import expected_conditions as EC
# from selenium.common.exceptions import TimeoutException, WebDriverException
# from webdriver_manager.chrome import ChromeDriverManager

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Backlinks Verifier API",
    description="API for verifying backlinks and search terms on web pages",
    version="1.0.0"
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure this for your frontend domain
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory storage for job results (use Redis in production)
job_results = {}

# Request/Response Models
class BacklinkRequest(BaseModel):
    urls: List[str] = Field(..., min_items=1, description="List of URLs to check")
    search_term: str = Field(..., min_length=1, description="Term to search for")
    search_in_html: bool = Field(default=True, description="Search in HTML source code")
    case_sensitive: bool = Field(default=False, description="Case sensitive search")
    timeout: int = Field(default=10, ge=5, le=30, description="Request timeout in seconds")
    delay: float = Field(default=1.0, ge=0.5, le=5.0, description="Delay between requests")
    max_workers: int = Field(default=3, ge=1, le=10, description="Max concurrent requests")
    debug: bool = Field(default=False, description="Enable debug logging")
    # use_browser: bool = Field(default=False, description="Use browser for JavaScript-rendered content")
    use_browser: bool = Field(default=False, description="Browser mode not available")
    
    @validator('urls')
    def validate_urls(cls, v):
        if not v:
            raise ValueError('At least one URL is required')
        return [url.strip() for url in v if url.strip()]

class BacklinkResult(BaseModel):
    url: str
    search_term: str
    verified: bool
    error: Optional[str] = None
    status_code: Optional[int] = None
    found_in: Optional[str] = None
    context: Optional[str] = None
    found_variation: Optional[str] = None
    warning: Optional[str] = None

class BacklinkResponse(BaseModel):
    job_id: str
    status: str  # "processing", "completed", "error"
    results: Optional[List[BacklinkResult]] = None
    summary: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    created_at: datetime
    completed_at: Optional[datetime] = None

class BacklinksVerifier:
    def __init__(self, timeout=10, delay=1, use_browser=False):
        self.timeout = timeout
        self.delay = delay
        self.use_browser = use_browser
        self.session = requests.Session()
        self.driver = None
        
        self.user_agents = [
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/121.0',
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15'
        ]
        
        retry_strategy = Retry(
            total=3,
            backoff_factor=2,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["HEAD", "GET", "OPTIONS"]
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        
        # Initialize browser if needed
        # if self.use_browser:
            # self.init_browser()
    
    # def init_browser(self):
    #     """Initialize Selenium WebDriver with stealth options"""
    #     try:
    #         chrome_options = Options()
            
    #         # Make browser look more human-like
    #         chrome_options.add_argument('--no-sandbox')
    #         chrome_options.add_argument('--disable-dev-shm-usage')
    #         chrome_options.add_argument('--disable-blink-features=AutomationControlled')
    #         chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
    #         chrome_options.add_experimental_option('useAutomationExtension', False)
    #         chrome_options.add_argument('--disable-web-security')
    #         chrome_options.add_argument('--allow-running-insecure-content')
    #         chrome_options.add_argument('--disable-features=VizDisplayCompositor')
            
    #         # Use a real user agent
    #         user_agent = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    #         chrome_options.add_argument(f'--user-agent={user_agent}')
            
    #         # Set window size to common resolution
    #         chrome_options.add_argument('--window-size=1920,1080')
            
    #         # Comment out headless mode to see what's happening (optional)
    #         # chrome_options.add_argument('--headless')  # Temporarily disable for debugging
            
    #         # Install and use ChromeDriver
    #         self.driver = webdriver.Chrome(
    #             service=webdriver.chrome.service.Service(ChromeDriverManager().install()),
    #             options=chrome_options
    #         )
            
    #         # Execute script to hide automation indicators
    #         self.driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            
    #         self.driver.set_page_load_timeout(self.timeout + 10)
    #         logger.info("Stealth browser initialized successfully")
            
    #     except Exception as e:
    #         logger.error(f"Failed to initialize browser: {e}")
    #         self.use_browser = False
    #         self.driver = None
    
    def close_browser(self):
        """Close the browser"""
        if self.driver:
            try:
                self.driver.quit()
            except:
                pass
            self.driver = None
    
    def normalize_url(self, url):
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url
        return url
    
    def fetch_page_content(self, url, max_retries=3):
        url = self.normalize_url(url)
        
        for attempt in range(max_retries):
            try:
                domain = urlparse(url).netloc.lower()
                
                headers = {
                    'User-Agent': random.choice(self.user_agents),
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                    'Accept-Language': 'en-US,en;q=0.9',
                    'Accept-Encoding': 'gzip, deflate, br',
                    'Connection': 'keep-alive',
                    'Upgrade-Insecure-Requests': '1',
                    'Cache-Control': 'max-age=0',
                    'DNT': '1'
                }
                
                if 'heylink.me' in domain or 'cloudflare' in domain:
                    headers.update({
                        'Sec-Fetch-Dest': 'document',
                        'Sec-Fetch-Mode': 'navigate', 
                        'Sec-Fetch-Site': 'none',
                        'Sec-Fetch-User': '?1',
                        'Referer': 'https://www.google.com/'
                    })
                
                if attempt > 0:
                    time.sleep(random.uniform(2, 5) if 'heylink.me' not in domain else random.uniform(3, 8))
                
                response = self.session.get(
                    url, 
                    headers=headers,
                    timeout=self.timeout,
                    allow_redirects=True,
                    verify=True
                )
                
                if response.status_code == 403:
                    if response.content:
                        try:
                            response.encoding = response.apparent_encoding or 'utf-8'
                            content_text = response.text.lower()
                            if len(content_text) > 100:
                                return {
                                    'success': True,
                                    'text_content': content_text,
                                    'html_content': content_text,
                                    'status_code': 403,
                                    'url': response.url,
                                    'warning': '403 Forbidden but content was accessible'
                                }
                        except:
                            pass
                    
                    if attempt < max_retries - 1:
                        continue
                    return {
                        'success': False,
                        'error': f'403 Forbidden - Access denied',
                        'status_code': 403,
                        'url': url
                    }
                
                if response.status_code == 429:
                    if attempt < max_retries - 1:
                        time.sleep(random.uniform(5, 10))
                        continue
                    return {
                        'success': False,
                        'error': f'429 Too Many Requests - Rate limited',
                        'status_code': 429,
                        'url': url
                    }
                
                response.raise_for_status()
                
                response.encoding = response.apparent_encoding or 'utf-8'
                soup = BeautifulSoup(response.content, 'html.parser')
                
                for script in soup(["script", "style"]):
                    script.decompose()
                
                text_content = soup.get_text(separator=' ', strip=True).lower()
                html_content = str(soup).lower()
                
                return {
                    'success': True,
                    'text_content': text_content,
                    'html_content': html_content,
                    'status_code': response.status_code,
                    'url': response.url
                }
                
            except requests.exceptions.Timeout:
                if attempt < max_retries - 1:
                    continue
                return {
                    'success': False,
                    'error': f'Request timeout after {self.timeout} seconds',
                    'status_code': None,
                    'url': url
                }
            except requests.exceptions.RequestException as e:
                if attempt < max_retries - 1:
                    continue
                return {
                    'success': False,
                    'error': str(e),
                    'status_code': getattr(e.response, 'status_code', None) if hasattr(e, 'response') and e.response else None,
                    'url': url
                }
        
        return {
            'success': False,
            'error': f'Failed after {max_retries} attempts',
            'status_code': None,
            'url': url
        }
    
    def verify_backlink(self, url, search_term, search_in_html=True, case_sensitive=False, debug=False):
        result = self.fetch_page_content(url)
        
        if not result['success']:
            return BacklinkResult(
                url=url,
                search_term=search_term,
                verified=False,
                error=result['error'],
                status_code=result.get('status_code'),
                found_in=None,
                context=None
            )
        
        # Enhanced search variations for domain/URL matching
        if case_sensitive:
            search_variations = [search_term]
        else:
            base_term = search_term.lower()
            search_variations = [base_term]  # Original term
            
            # If it's a URL/domain, create comprehensive variations
            if any(indicator in base_term for indicator in ['http', 'www.', '.com', '.in', '.org', '.net']):
                # Clean the URL step by step
                clean_url = base_term
                clean_url = clean_url.replace('https://', '').replace('http://', '')
                clean_url = clean_url.replace('www.', '')
                clean_url = clean_url.rstrip('/')  # Remove trailing slash
                
                # Add ALL possible variations
                search_variations.extend([
                    # Original formats
                    base_term,  # https://www.gulbhahar.com
                    search_term.upper(),
                    search_term.title(),
                    
                    # Without protocols
                    clean_url,  # gulbhahar.com
                    f"www.{clean_url}",  # www.gulbhahar.com
                    
                    # With different protocols
                    f"https://{clean_url}",  # https://gulbhahar.com
                    f"http://{clean_url}",   # http://gulbhahar.com
                    f"https://www.{clean_url}",  # https://www.gulbhahar.com
                    f"http://www.{clean_url}",   # http://www.gulbhahar.com
                    
                    # With trailing slashes
                    f"{clean_url}/",
                    f"www.{clean_url}/",
                    f"https://{clean_url}/",
                    f"http://{clean_url}/",
                    f"https://www.{clean_url}/",
                    f"http://www.{clean_url}/",
                ])
                
                # Extract domain name for brand matching
                if '.' in clean_url:
                    domain_parts = clean_url.split('/')[0].split('.')  # Get just domain, no paths
                    if len(domain_parts) >= 2:
                        brand_name = domain_parts[0]  # gulbhahar
                        search_variations.extend([
                            brand_name,  # gulbhahar
                            brand_name.upper(),  # GULBHAHAR
                            brand_name.title(),  # Gulbhahar
                        ])
                
                # Add partial domain matching (for subpaths)
                base_domain = clean_url.split('/')[0]  # gulbhahar.com (no paths)
                search_variations.extend([
                    base_domain,
                    f"www.{base_domain}",
                    f"https://{base_domain}",
                    f"https://www.{base_domain}",
                ])
                
            else:
                # If it's just text (like "gulbhahar"), add URL variations
                search_variations.extend([
                    f"{base_term}.com",
                    f"www.{base_term}.com",
                    f"https://{base_term}.com",
                    f"https://www.{base_term}.com",
                    f"http://{base_term}.com",
                    f"http://www.{base_term}.com",
                    base_term.upper(),
                    base_term.title(),
                ])
            
            # Remove duplicates while preserving order
            search_variations = list(dict.fromkeys(search_variations))
        
        text_content = result['text_content']
        html_content = result['html_content']
        
        # DEBUG: Log what we're actually getting from the page
        if debug:
            logger.info(f"=== DEBUG for {url} ===")
            logger.info(f"Text content length: {len(text_content)}")
            logger.info(f"Text preview (first 500 chars): {text_content[:500]}")
            logger.info(f"Search variations: {search_variations[:10]}")  # First 10 only
            logger.info(f"=== END DEBUG ===")
        
        found_in_text = False
        found_in_html = False
        context = None
        found_variation = None
        
        # Search in text content with better context extraction
        for variation in search_variations:
            if variation in text_content:
                found_in_text = True
                found_variation = variation
                
                # Extract context around the found term
                index = text_content.find(variation)
                if index != -1:
                    # Get more context for better understanding
                    start = max(0, index - 200)
                    end = min(len(text_content), index + len(variation) + 200)
                    raw_context = text_content[start:end]
                    
                    # Clean up the context
                    context = ' '.join(raw_context.split())
                    
                    # Highlight the found term in context (optional)
                    if len(context) > 50:
                        # Truncate if too long but keep the found term visible
                        term_pos = context.lower().find(variation.lower())
                        if term_pos > 150:
                            context = "..." + context[term_pos-100:term_pos+200] + "..."
                break
        
        # Search in HTML if not found in text
        if not found_in_text and search_in_html:
            for variation in search_variations:
                if variation in html_content:
                    found_in_html = True
                    found_variation = variation
                    
                    # Try to extract meaningful context from HTML
                    index = html_content.find(variation)
                    if index != -1:
                        start = max(0, index - 150)
                        end = min(len(html_content), index + len(variation) + 150)
                        html_context = html_content[start:end]
                        
                        # Remove HTML tags for better readability
                        import re
                        clean_context = re.sub(r'<[^>]+>', ' ', html_context)
                        clean_context = ' '.join(clean_context.split())
                        
                        if clean_context.strip():
                            context = f"HTML: {clean_context}"
                        else:
                            context = "Found in HTML source code"
                    else:
                        context = "Found in HTML attributes or tags"
                    break
        
        # Additional debugging for failed matches
        if not found_in_text and not found_in_html and debug:
            logger.info(f"❌ NO MATCH found for {search_term} in {url}")
            logger.info(f"Checked {len(search_variations)} variations")
            # Check if any partial matches exist
            main_term = search_variations[0] if search_variations else search_term.lower()
            if len(main_term) > 3:
                partial_check = main_term[:len(main_term)//2]  # Check first half
                if partial_check in text_content:
                    logger.info(f"⚠️ Partial match found for '{partial_check}' - might be dynamic content")
        
        # Check current page URL for domain matches
        if not found_in_text and not found_in_html:
            current_url = result.get('url', url).lower()
            for variation in search_variations:
                if variation in current_url:
                    found_in_html = True
                    found_variation = variation
                    context = f"Found in page URL: {current_url}"
                    break
        
        # Additional check: Look for domain name in any links on the page
        if not found_in_text and not found_in_html and search_in_html:
            # Extract domain from search term for link checking
            search_domain = search_term.lower()
            if 'http' in search_domain:
                search_domain = search_domain.replace('https://', '').replace('http://', '').replace('www.', '').split('/')[0]
            
            # Look for links containing the domain
            link_pattern = f'href=.*{search_domain.split(".")[0]}'
            if search_domain.split('.')[0] in html_content and len(search_domain.split('.')[0]) > 3:
                found_in_html = True
                found_variation = search_domain
                context = f"Domain found in page links"
        
        verified = found_in_text or found_in_html
        found_in = "Page Text" if found_in_text else ("HTML/Links" if found_in_html else None)
        
        return BacklinkResult(
            url=url,
            search_term=search_term,
            verified=verified,
            error=None,
            status_code=result['status_code'],
            found_in=found_in,
            context=context[:500] if context else None,  # Limit context length
            found_variation=found_variation,
            warning=result.get('warning')
        )
        result = self.fetch_page_content(url)
        
        if not result['success']:
            return BacklinkResult(
                url=url,
                search_term=search_term,
                verified=False,
                error=result['error'],
                status_code=result.get('status_code'),
                found_in=None,
                context=None
            )
        
        # Enhanced search variations for domain/URL matching
        if case_sensitive:
            search_variations = [search_term]
        else:
            base_term = search_term.lower()
            search_variations = [base_term]  # Original term
            
            # If it's a URL/domain, create comprehensive variations
            if any(indicator in base_term for indicator in ['http', 'www.', '.com', '.in', '.org', '.net']):
                # Clean the URL step by step
                clean_url = base_term
                clean_url = clean_url.replace('https://', '').replace('http://', '')
                clean_url = clean_url.replace('www.', '')
                clean_url = clean_url.rstrip('/')  # Remove trailing slash
                
                # Add ALL possible variations
                search_variations.extend([
                    # Original formats
                    base_term,  # https://www.gulbhahar.com
                    search_term.upper(),
                    search_term.title(),
                    
                    # Without protocols
                    clean_url,  # gulbhahar.com
                    f"www.{clean_url}",  # www.gulbhahar.com
                    
                    # With different protocols
                    f"https://{clean_url}",  # https://gulbhahar.com
                    f"http://{clean_url}",   # http://gulbhahar.com
                    f"https://www.{clean_url}",  # https://www.gulbhahar.com
                    f"http://www.{clean_url}",   # http://www.gulbhahar.com
                    
                    # With trailing slashes
                    f"{clean_url}/",
                    f"www.{clean_url}/",
                    f"https://{clean_url}/",
                    f"http://{clean_url}/",
                    f"https://www.{clean_url}/",
                    f"http://www.{clean_url}/",
                ])
                
                # Extract domain name for brand matching
                if '.' in clean_url:
                    domain_parts = clean_url.split('/')[0].split('.')  # Get just domain, no paths
                    if len(domain_parts) >= 2:
                        brand_name = domain_parts[0]  # gulbhahar
                        search_variations.extend([
                            brand_name,  # gulbhahar
                            brand_name.upper(),  # GULBHAHAR
                            brand_name.title(),  # Gulbhahar
                        ])
                
                # Add partial domain matching (for subpaths)
                base_domain = clean_url.split('/')[0]  # gulbhahar.com (no paths)
                search_variations.extend([
                    base_domain,
                    f"www.{base_domain}",
                    f"https://{base_domain}",
                    f"https://www.{base_domain}",
                ])
                
            else:
                # If it's just text (like "gulbhahar"), add URL variations
                search_variations.extend([
                    f"{base_term}.com",
                    f"www.{base_term}.com",
                    f"https://{base_term}.com",
                    f"https://www.{base_term}.com",
                    f"http://{base_term}.com",
                    f"http://www.{base_term}.com",
                    base_term.upper(),
                    base_term.title(),
                ])
            
            # Remove duplicates while preserving order
            search_variations = list(dict.fromkeys(search_variations))
        
        text_content = result['text_content']
        html_content = result['html_content']
        
        found_in_text = False
        found_in_html = False
        context = None
        found_variation = None
        
        # Search in text content with better context extraction
        for variation in search_variations:
            if variation in text_content:
                found_in_text = True
                found_variation = variation
                
                # Extract context around the found term
                index = text_content.find(variation)
                if index != -1:
                    # Get more context for better understanding
                    start = max(0, index - 200)
                    end = min(len(text_content), index + len(variation) + 200)
                    raw_context = text_content[start:end]
                    
                    # Clean up the context
                    context = ' '.join(raw_context.split())
                    
                    # Highlight the found term in context (optional)
                    if len(context) > 50:
                        # Truncate if too long but keep the found term visible
                        term_pos = context.lower().find(variation.lower())
                        if term_pos > 150:
                            context = "..." + context[term_pos-100:term_pos+200] + "..."
                break
        
        # Search in HTML if not found in text
        if not found_in_text and search_in_html:
            for variation in search_variations:
                if variation in html_content:
                    found_in_html = True
                    found_variation = variation
                    
                    # Try to extract meaningful context from HTML
                    index = html_content.find(variation)
                    if index != -1:
                        start = max(0, index - 150)
                        end = min(len(html_content), index + len(variation) + 150)
                        html_context = html_content[start:end]
                        
                        # Remove HTML tags for better readability
                        import re
                        clean_context = re.sub(r'<[^>]+>', ' ', html_context)
                        clean_context = ' '.join(clean_context.split())
                        
                        if clean_context.strip():
                            context = f"HTML: {clean_context}"
                        else:
                            context = "Found in HTML source code"
                    else:
                        context = "Found in HTML attributes or tags"
                    break
        
        # Check current page URL for domain matches
        if not found_in_text and not found_in_html:
            current_url = result.get('url', url).lower()
            for variation in search_variations:
                if variation in current_url:
                    found_in_html = True
                    found_variation = variation
                    context = f"Found in page URL: {current_url}"
                    break
        
        # Additional check: Look for domain name in any links on the page
        if not found_in_text and not found_in_html and search_in_html:
            # Extract domain from search term for link checking
            search_domain = search_term.lower()
            if 'http' in search_domain:
                search_domain = search_domain.replace('https://', '').replace('http://', '').replace('www.', '').split('/')[0]
            
            # Look for links containing the domain
            link_pattern = f'href=.*{search_domain.split(".")[0]}'
            if search_domain.split('.')[0] in html_content and len(search_domain.split('.')[0]) > 3:
                found_in_html = True
                found_variation = search_domain
                context = f"Domain found in page links"
        
        verified = found_in_text or found_in_html
        found_in = "Page Text" if found_in_text else ("HTML/Links" if found_in_html else None)
        
        return BacklinkResult(
            url=url,
            search_term=search_term,
            verified=verified,
            error=None,
            status_code=result['status_code'],
            found_in=found_in,
            context=context[:500] if context else None,  # Limit context length
            found_variation=found_variation,
            warning=result.get('warning')
        )
    
    def verify_multiple_backlinks(self, urls, search_term, search_in_html=True, case_sensitive=False, max_workers=3, debug=False, use_browser=False):
        # Update browser setting
        if use_browser and not self.use_browser:
            self.use_browser = use_browser
            self.init_browser()
        
        results = []
        
        # For browser mode, use single-threaded to avoid driver conflicts
        if self.use_browser:
            max_workers = 1
            logger.info("Using browser mode - single-threaded processing")
        
        max_workers = min(max_workers, 3)
        
        if max_workers == 1:
            # Single-threaded processing for browser mode
            for url in urls:
                if url.strip():
                    try:
                        result = self.verify_backlink(url.strip(), search_term, search_in_html, case_sensitive, debug)
                        results.append(result)
                    except Exception as e:
                        results.append(BacklinkResult(
                            url=url.strip(),
                            search_term=search_term,
                            verified=False,
                            error=f"Processing error: {str(e)}",
                            status_code=None,
                            found_in=None,
                            context=None
                        ))
                    
                    # Add delay between requests
                    time.sleep(random.uniform(self.delay, self.delay + 1))
        else:
            # Multi-threaded processing for requests mode
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_url = {
                    executor.submit(self.verify_backlink, url.strip(), search_term, search_in_html, case_sensitive, debug): url.strip() 
                    for url in urls if url.strip()
                }
                
                for i, future in enumerate(as_completed(future_to_url)):
                    try:
                        result = future.result()
                        results.append(result)
                    except Exception as e:
                        url = future_to_url[future]
                        results.append(BacklinkResult(
                            url=url,
                            search_term=search_term,
                            verified=False,
                            error=f"Processing error: {str(e)}",
                            status_code=None,
                            found_in=None,
                            context=None
                        ))
                    
                    base_delay = self.delay
                    progressive_delay = base_delay + (i * 0.1)
                    time.sleep(random.uniform(progressive_delay, progressive_delay + 1))
        
        # Clean up browser
        if self.use_browser:
            self.close_browser()
        
        return results

async def process_backlinks(job_id: str, request: BacklinkRequest):
    """Background task to process backlinks"""
    try:
        logger.info(f"Starting job {job_id}")
        job_results[job_id]['status'] = 'processing'
        
        verifier = BacklinksVerifier(timeout=request.timeout, delay=request.delay, use_browser=request.use_browser)
        
        results = verifier.verify_multiple_backlinks(
            request.urls,
            request.search_term,
            request.search_in_html,
            request.case_sensitive,
            request.max_workers,
            request.debug,
            request.use_browser
        )
        
        verified_count = sum(1 for r in results if r.verified)
        error_count = sum(1 for r in results if r.error)
        total_count = len(results)
        
        summary = {
            'total_urls': total_count,
            'verified_count': verified_count,
            'error_count': error_count,
            'success_rate': round((verified_count / total_count) * 100, 1) if total_count > 0 else 0
        }
        
        job_results[job_id].update({
            'status': 'completed',
            'results': results,
            'summary': summary,
            'completed_at': datetime.now()
        })
        
        logger.info(f"Job {job_id} completed successfully")
        
    except Exception as e:
        logger.error(f"Job {job_id} failed: {str(e)}")
        job_results[job_id].update({
            'status': 'error',
            'error': str(e),
            'completed_at': datetime.now()
        })

# API Endpoints
@app.get("/")
async def root():
    return {
        "message": "Backlinks Verifier API",
        "version": "1.0.0",
        "endpoints": {
            "verify": "POST /verify",
            "verify_sync": "POST /verify/sync", 
            "verify_stream": "POST /verify/stream",
            "upload_file": "POST /upload-file",
            "status": "GET /status/{job_id}",
            "health": "GET /health"
        }
    }

@app.get("/health")
async def health_check():
    return {"status": "healthy", "timestamp": datetime.now()}

@app.post("/verify", response_model=BacklinkResponse)
async def verify_backlinks_async(request: BacklinkRequest, background_tasks: BackgroundTasks):
    """Start async backlinks verification job"""
    job_id = str(uuid.uuid4())
    
    job_results[job_id] = {
        'job_id': job_id,
        'status': 'pending',
        'results': None,
        'summary': None,
        'error': None,
        'created_at': datetime.now(),
        'completed_at': None
    }
    
    background_tasks.add_task(process_backlinks, job_id, request)
    return BacklinkResponse(**job_results[job_id])

@app.post("/verify/sync", response_model=BacklinkResponse)
async def verify_backlinks_sync(request: BacklinkRequest):
    """Synchronous backlinks verification (for small batches)"""
    if len(request.urls) > 10:
        raise HTTPException(
            status_code=400, 
            detail="Synchronous verification limited to 10 URLs. Use async endpoint for larger batches."
        )
    
    try:
        verifier = BacklinksVerifier(timeout=request.timeout, delay=request.delay, use_browser=request.use_browser)
        
        results = verifier.verify_multiple_backlinks(
            request.urls,
            request.search_term,
            request.search_in_html,
            request.case_sensitive,
            request.max_workers,
            request.debug,
            request.use_browser
        )
        
        verified_count = sum(1 for r in results if r.verified)
        error_count = sum(1 for r in results if r.error)
        total_count = len(results)
        
        summary = {
            'total_urls': total_count,
            'verified_count': verified_count,
            'error_count': error_count,
            'success_rate': round((verified_count / total_count) * 100, 1) if total_count > 0 else 0
        }
        
        return BacklinkResponse(
            job_id=str(uuid.uuid4()),
            status='completed',
            results=results,
            summary=summary,
            created_at=datetime.now(),
            completed_at=datetime.now()
        )
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/status/{job_id}", response_model=BacklinkResponse)
async def get_job_status(job_id: str):
    """Get job status and results"""
    if job_id not in job_results:
        raise HTTPException(status_code=404, detail="Job not found")
    
    return BacklinkResponse(**job_results[job_id])

@app.post("/upload-file")
async def upload_file(file: UploadFile = File(...)):
    """Upload CSV or Excel file and extract URLs from 'submitted url' column"""
    if not file.filename.lower().endswith(('.csv', '.xlsx', '.xls')):
        raise HTTPException(status_code=400, detail="File must be CSV, XLSX, or XLS")
    
    try:
        contents = await file.read()
        
        # Handle Excel files
        if file.filename.lower().endswith(('.xlsx', '.xls')):
            df = pd.read_excel(io.BytesIO(contents))
        else:
            # Handle CSV files
            df = pd.read_csv(io.StringIO(contents.decode('utf-8')))
        
        # Look for URL column
        url_columns = []
        for col in df.columns:
            col_lower = col.lower().strip()
            if any(keyword in col_lower for keyword in [
                'submitted url', 'submitted_url', 'submittedurl',
                'url', 'website', 'link', 'domain', 'site'
            ]):
                url_columns.append(col)
        
        if not url_columns:
            available_columns = list(df.columns)
            raise HTTPException(
                status_code=400, 
                detail=f"No URL column found. Available columns: {available_columns}. "
                       f"Expected column names: 'submitted url', 'url', 'website', 'link', etc."
            )
        
        # Use the first matching column (prioritize 'submitted url')
        url_column = url_columns[0]
        for col in url_columns:
            if 'submitted' in col.lower():
                url_column = col
                break
        
        # Extract and clean URLs
        urls = df[url_column].dropna().astype(str).tolist()
        
        cleaned_urls = []
        for url in urls:
            url = url.strip()
            if url and url.lower() not in ['nan', 'none', '', 'null']:
                if not url.startswith(('http://', 'https://')) and '.' in url:
                    url = 'https://' + url
                cleaned_urls.append(url)
        
        if not cleaned_urls:
            raise HTTPException(
                status_code=400, 
                detail=f"No valid URLs found in column '{url_column}'"
            )
        
        return {
            "message": f"Successfully loaded {len(cleaned_urls)} URLs from column '{url_column}'",
            "urls": cleaned_urls,
            "total_count": len(cleaned_urls),
            "column_used": url_column,
            "file_type": "Excel" if file.filename.lower().endswith(('.xlsx', '.xls')) else "CSV",
            "available_columns": list(df.columns)
        }
        
    except pd.errors.EmptyDataError:
        raise HTTPException(status_code=400, detail="File is empty or corrupted")
    except pd.errors.ParserError as e:
        raise HTTPException(status_code=400, detail=f"Error parsing file: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error processing file: {str(e)}")

@app.post("/verify/stream")
async def verify_backlinks_stream(request: BacklinkRequest):
    """Stream verification results live as each URL is processed"""
    
    def generate_results() -> Iterator[str]:
        try:
            verifier = BacklinksVerifier(
                timeout=request.timeout, 
                delay=request.delay, 
                use_browser=request.use_browser
            )
            
            # Send initial status
            yield f"data: {json.dumps({'status': 'started', 'total_urls': len(request.urls)})}\n\n"
            
            results = []
            verified_count = 0
            error_count = 0
            
            # Process URLs one by one
            for i, url in enumerate(request.urls):
                if not url.strip():
                    continue
                    
                try:
                    # Send processing status
                    yield f"data: {json.dumps({'status': 'processing', 'current_url': url, 'progress': i+1, 'total': len(request.urls)})}\n\n"
                    
                    # Verify the URL
                    result = verifier.verify_backlink(
                        url.strip(), 
                        request.search_term, 
                        request.search_in_html, 
                        request.case_sensitive, 
                        request.debug
                    )
                    
                    # Convert to dict for JSON serialization
                    result_dict = {
                        'url': result.url,
                        'search_term': result.search_term,
                        'verified': result.verified,
                        'error': result.error,
                        'status_code': result.status_code,
                        'found_in': result.found_in,
                        'context': result.context,
                        'found_variation': result.found_variation,
                        'warning': result.warning
                    }
                    
                    results.append(result_dict)
                    
                    if result.verified:
                        verified_count += 1
                    if result.error:
                        error_count += 1
                    
                    # Send immediate result
                    yield f"data: {json.dumps({'type': 'result', 'result': result_dict, 'progress': i+1})}\n\n"
                    
                    # Add delay between requests
                    time.sleep(random.uniform(request.delay, request.delay + 1))
                    
                except Exception as e:
                    error_result = {
                        'url': url.strip(),
                        'search_term': request.search_term,
                        'verified': False,
                        'error': f"Processing error: {str(e)}",
                        'status_code': None,
                        'found_in': None,
                        'context': None,
                        'found_variation': None,
                        'warning': None
                    }
                    
                    results.append(error_result)
                    error_count += 1
                    
                    # Send error result
                    yield f"data: {json.dumps({'type': 'result', 'result': error_result, 'progress': i+1})}\n\n"
            
            # Send final summary
            summary = {
                'total_urls': len(request.urls),
                'verified_count': verified_count,
                'error_count': error_count,
                'success_rate': round((verified_count / len(request.urls)) * 100, 1) if len(request.urls) > 0 else 0
            }
            
            yield f"data: {json.dumps({'status': 'completed', 'summary': summary, 'all_results': results})}\n\n"
            
            # Clean up browser
            if verifier.use_browser:
                verifier.close_browser()
                
        except Exception as e:
            yield f"data: {json.dumps({'status': 'error', 'error': str(e)})}\n\n"
    
    return StreamingResponse(
        generate_results(),
        media_type="text/plain",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Content-Type": "text/event-stream"
        }
    )
    """Debug endpoint to see what content the API extracts from a page"""
    try:
        verifier = BacklinksVerifier()
        result = verifier.fetch_page_content(url)
        
        if result['success']:
            return {
                "url": url,
                "status_code": result['status_code'],
                "text_content_length": len(result['text_content']),
                "text_preview": result['text_content'][:1000],  # First 1000 chars
                "html_content_length": len(result['html_content']),
                "html_preview": result['html_content'][:1000],  # First 1000 chars
                "final_url": result.get('url', url)
            }
        else:
            return {
                "url": url,
                "error": result['error'],
                "status_code": result.get('status_code')
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/debug-page")
async def debug_page_content(url: str):
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)