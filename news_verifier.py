
import requests
import feedparser

from newsapi import NewsApiClient
from bs4 import BeautifulSoup
import re
from datetime import datetime, timedelta
from fuzzywuzzy import fuzz
import json
import logging
from urllib.parse import urljoin, urlparse
from config import Config
import time

class NewsVerifier:
    def __init__(self):
        self.config = Config()
        self.newsapi = None
        if self.config.NEWS_API_KEY and self.config.NEWS_API_KEY != '8b335dc6442443eca479b1bf193cfc68':
            self.newsapi = NewsApiClient(api_key=self.config.NEWS_API_KEY)
        self.logger = logging.getLogger(__name__)
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
        # Domain reputation weights (higher is more reputable)
        self.domain_weights = {
            'bbc.co.uk': 1.0,
            'bbc.com': 1.0,
            'reuters.com': 1.0,
            'apnews.com': 0.95,
            'npr.org': 0.9,
            'cnn.com': 0.85,
            'thehindu.com': 0.85,
            'indiatimes.com': 0.75,
            'hindustantimes.com': 0.75,
            'indianexpress.com': 0.8,
            'ndtv.com': 0.75,
            'news18.com': 0.7,
            'firstpost.com': 0.65,
            'deccanherald.com': 0.7,
            'republicworld.com': 0.4
        }
        
    def verify_headline(self, headline):
        """Main verification function"""
        verification_result = {
            'headline': headline,
            'authenticity_score': 0,
            'verification_status': 'Unknown',
            'sources_found': [],
            'similar_headlines': [],
            'summary': {
                'what_happened': '',
                'when_happened': '',
                'where_happened': '',
                'why_happened': ''
            },
            'details': {
                'total_sources_checked': 0,
                'matching_sources': 0,
                'fact_check_results': [],
                'verification_method': [],
                'reasoning': []
            }
        }
        
        try:
            normalized_headline = self._normalize_text(headline)
            # Step 1: Search using NewsAPI (if available)
            if self.newsapi:
                verification_result = self._verify_with_newsapi(normalized_headline, verification_result)
            
            # Step 2: Search using RSS feeds and web scraping
            verification_result = self._verify_with_rss_feeds(normalized_headline, verification_result)
            
            # Step 3: Check fact-checking websites
            verification_result = self._check_fact_checking_sites(normalized_headline, verification_result)
            
            # Step 4: Calculate final authenticity score
            verification_result = self._calculate_authenticity_score(verification_result)
            
            # Step 5: Generate summary
            verification_result = self._generate_summary(verification_result)
            
        except Exception as e:
            self.logger.error(f"Error in headline verification: {str(e)}")
            verification_result['verification_status'] = 'Error'
            verification_result['error'] = str(e)
        
        return verification_result
    
    def _verify_with_newsapi(self, headline, result):
        """Verify headline using NewsAPI"""
        try:
            self.logger.info("Verifying with NewsAPI...")
            result['details']['verification_method'].append('NewsAPI')
            
            # Extract keywords from headline for search
            keywords = self._extract_keywords(headline)
            
            # Ensure we have a valid search query
            if not keywords or len(keywords) < 1:
                # Fallback: use first few words of headline
                words = headline.split()[:3]
                search_query = ' '.join(words)
            else:
                # Use only the most important keywords (max 3) to avoid overly specific searches
                search_query = ' '.join(keywords[:3])
            
            self.logger.info(f"Searching NewsAPI with query: '{search_query}'")
            
            # Search for articles
            date_from = (datetime.utcnow() - timedelta(days=14)).strftime('%Y-%m-%d')
            articles = self.newsapi.get_everything(
                q=search_query,
                language='en',
                sort_by='relevancy',
                from_param=date_from,
                page_size=50
            )
            
            result['details']['total_sources_checked'] += len(articles['articles'])
            
            seen_urls = set()
            for article in articles['articles']:
                try:
                    # Use multiple similarity methods for better matching
                    title = article['title'].lower()
                    headline_lower = headline.lower()
                    
                    ratio = fuzz.ratio(headline_lower, title)
                    partial = fuzz.partial_ratio(headline_lower, title)
                    token_sort = fuzz.token_sort_ratio(headline_lower, title)
                    
                    # Use the highest similarity score
                    similarity = max(ratio, partial, token_sort)
                    # Skip low similarity early
                    if similarity < 55:
                        continue

                    # Basic recency check (prefer last 21 days)
                    published_at = article.get('publishedAt')
                    is_recent = True
                    try:
                        if published_at:
                            # NewsAPI uses ISO timestamps
                            published_dt = datetime.fromisoformat(published_at.replace('Z', '+00:00'))
                            is_recent = (datetime.utcnow() - published_dt.replace(tzinfo=None)) <= timedelta(days=21)
                    except Exception:
                        pass

                    # Deduplicate by URL
                    url = article.get('url')
                    if not url or url in seen_urls:
                        continue
                    seen_urls.add(url)

                    # Domain reputation weight
                    domain = urlparse(url).netloc.replace('www.', '')
                    reputation_weight = self.domain_weights.get(domain, 0.5)

                    self.logger.info(f"Article similarity: {similarity}% (ratio:{ratio}, partial:{partial}, token:{token_sort}) - {article['title'][:50]}...")
                    
                    if similarity >= 62 and is_recent:
                        self.logger.info(f"Adding matching source: {article['source']['name']}")
                        result['sources_found'].append({
                            'source': article['source']['name'],
                            'title': article['title'],
                            'url': url,
                            'published_at': published_at,
                            'similarity_score': similarity,
                            'description': article.get('description', ''),
                            'domain': domain,
                            'reputation_weight': reputation_weight
                        })
                        
                        result['similar_headlines'].append({
                            'title': article['title'],
                            'similarity': similarity,
                            'source': article['source']['name']
                        })
                        
                        result['details']['matching_sources'] += 1
                except Exception as e:
                    self.logger.error(f"Error processing article: {e}")
                    continue
            
        except Exception as e:
            self.logger.error(f"NewsAPI verification failed: {str(e)}")
            result['details']['newsapi_error'] = str(e)
        
        return result
    
    def _verify_with_rss_feeds(self, headline, result):
        """Verify headline using RSS feeds and web scraping"""
        try:
            self.logger.info("Verifying with RSS feeds...")
            result['details']['verification_method'].append('RSS_Feeds')
            
            keywords = self._extract_keywords(headline)
            
            for feed_url in self.config.NEWS_SOURCES:
                try:
                    feed = feedparser.parse(feed_url)
                    result['details']['total_sources_checked'] += len(feed.entries)
                    
                    for entry in feed.entries:
                        # Use multiple similarity methods for better matching
                        entry_title = entry.title.lower()
                        headline_lower = headline.lower()
                        
                        ratio = fuzz.ratio(headline_lower, entry_title)
                        partial = fuzz.partial_ratio(headline_lower, entry_title)
                        token_sort = fuzz.token_sort_ratio(headline_lower, entry_title)
                        
                        # Use the highest similarity score
                        similarity = max(ratio, partial, token_sort)
                        if similarity < 55:
                            continue

                        # Recency filter: last 21 days if published
                        published = entry.get('published', '')
                        is_recent = True
                        try:
                            if published:
                                parsed = feedparser.parse(published)
                                # feedparser can't parse single dates directly; skip strict check
                                is_recent = True
                        except Exception:
                            pass

                        url = entry.link
                        domain = urlparse(url).netloc.replace('www.', '')
                        reputation_weight = self.domain_weights.get(domain, 0.5)

                        if is_recent:
                            result['sources_found'].append({
                                'source': feed.feed.get('title', 'RSS Feed'),
                                'title': entry.title,
                                'url': url,
                                'published_at': published,
                                'similarity_score': similarity,
                                'description': entry.get('summary', ''),
                                'domain': domain,
                                'reputation_weight': reputation_weight
                            })
                            
                            result['similar_headlines'].append({
                                'title': entry.title,
                                'similarity': similarity,
                                'source': feed.feed.get('title', 'RSS Feed')
                            })
                            
                            result['details']['matching_sources'] += 1
                
                except Exception as e:
                    self.logger.warning(f"Failed to parse RSS feed {feed_url}: {str(e)}")
                    continue
                    
        except Exception as e:
            self.logger.error(f"RSS feed verification failed: {str(e)}")
        
        return result
    
    def _check_fact_checking_sites(self, headline, result):
        """Check fact-checking websites"""
        try:
            self.logger.info("Checking fact-checking sites...")
            result['details']['verification_method'].append('Fact_Check')
            
            keywords = self._extract_keywords(headline)
            search_terms = ' '.join(keywords[:3])
            
            # Try Google Fact Check API first if available
            if self.config.GOOGLE_API_KEY:
                try:
                    api_url = (
                        f"https://factchecktools.googleapis.com/v1alpha1/claims:search?query={requests.utils.quote(search_terms)}"
                        f"&pageSize=5&key={self.config.GOOGLE_API_KEY}"
                    )
                    resp = self.session.get(api_url, timeout=10)
                    if resp.status_code == 200:
                        data = resp.json()
                        claims = data.get('claims', [])
                        if claims:
                            result['details']['fact_check_results'].append({
                                'site': 'Google Fact Check API',
                                'results_found': len(claims),
                                'status': 'Found related fact-checks'
                            })
                            # If any claim rated false, record
                            for claim in claims:
                                reviews = claim.get('claimReview', [])
                                for review in reviews:
                                    text_rating = review.get('textualRating', '').lower()
                                    publisher = review.get('publisher', {}).get('name', '')
                                    url = review.get('url')
                                    result['details']['fact_check_results'].append({
                                        'site': publisher or 'Fact-check',
                                        'rating': text_rating,
                                        'url': url
                                    })
                except Exception as e:
                    self.logger.warning(f"Fact Check API failed: {e}")

            for fact_site in self.config.FACT_CHECK_SOURCES:
                try:
                    # Simple Google search for fact-check results
                    search_url = f"https://www.google.com/search?q=site:{fact_site}+{requests.utils.quote(search_terms)}"
                    response = self.session.get(search_url, timeout=10)
                    if response.status_code == 200:
                        soup = BeautifulSoup(response.content, 'html.parser')
                        search_results = soup.find_all('h3')
                        
                        if len(search_results) > 0:
                            result['details']['fact_check_results'].append({
                                'site': fact_site,
                                'results_found': len(search_results),
                                'status': 'Found related fact-checks'
                            })
                
                except Exception as e:
                    self.logger.warning(f"Failed to check {fact_site}: {str(e)}")
                    continue
                    
        except Exception as e:
            self.logger.error(f"Fact-checking failed: {str(e)}")
        
        return result
    
    def _calculate_authenticity_score(self, result):
        """Calculate overall authenticity score"""
        score = 0
        
        # Base score from number of matching sources
        matching_sources = result['details']['matching_sources']
        if matching_sources >= 4:
            score += 45
        elif matching_sources >= 3:
            score += 35
        elif matching_sources >= 2:
            score += 25
        elif matching_sources >= 1:
            score += 12
        
        # Score from similarity scores
        if result['similar_headlines']:
            avg_similarity = sum(h['similarity'] for h in result['similar_headlines']) / len(result['similar_headlines'])
            score += int(min(40, max(0, (avg_similarity - 55) * 0.8)))  # scaled from threshold
        
        # Score from reputable sources (domain-based weighting)
        if result['sources_found']:
            max_rep_weight = max(s.get('reputation_weight', 0.5) for s in result['sources_found'])
            score += int(15 * max_rep_weight)
        
        # Score from fact-checking results
        if result['details']['fact_check_results']:
            # Penalize if any explicit false ratings found
            has_false = any(
                (isinstance(item, dict) and str(item.get('rating', '')).lower() in {'false', 'fake', 'pants on fire'})
                for item in result['details']['fact_check_results']
            )
            if has_false:
                score -= 20
                result['details']['reasoning'].append('Fact-check indicates falsehood')
            else:
                score += 8
        
        # Ensure score is within 0-100 range
        score = min(100, max(0, score))
        result['authenticity_score'] = score
        
        # Determine verification status
        if score >= 80:
            result['verification_status'] = 'Highly Likely True'
        elif score >= 60:
            result['verification_status'] = 'Likely True'
        elif score >= 40:
            result['verification_status'] = 'Possibly True'
        elif score >= 20:
            result['verification_status'] = 'Questionable'
        else:
            result['verification_status'] = 'Likely False or Unverified'
        
        return result
    
    def _generate_summary(self, result):
        """Generate What, When, Where, Why summary"""
        if not result['sources_found']:
            return result
        
        # Analyze the most similar source
        best_source = max(result['sources_found'], key=lambda x: (x.get('reputation_weight', 0.5), x['similarity_score']))
        
        # Extract What happened
        result['summary']['what_happened'] = f"Based on verification, the headline appears to be related to: {best_source['title']}"
        
        # Extract When happened
        if best_source.get('published_at'):
            try:
                pub_date = best_source['published_at']
                if isinstance(pub_date, str):
                    # Try to parse the date
                    result['summary']['when_happened'] = f"Originally reported around: {pub_date}"
                else:
                    result['summary']['when_happened'] = f"Originally reported around: {pub_date}"
            except:
                result['summary']['when_happened'] = "Date information not available"
        
        # Extract Where happened
        description = best_source.get('description', '')
        if description:
            # Simple location extraction (can be improved with NLP)
            locations = re.findall(r'\b(?:in|at)\s+([A-Z][a-zA-Z\s]+?)(?:[,.]|\s+(?:said|reported|according))', description)
            if locations:
                result['summary']['where_happened'] = f"Location mentioned: {locations[0].strip()}"
            else:
                result['summary']['where_happened'] = "Location information not clearly specified"
        
        # Extract Why happened
        if description and len(description) > 50:
            result['summary']['why_happened'] = f"Context: {description[:200]}..."
        else:
            result['summary']['why_happened'] = "Additional context not available from sources"
        
        return result
    
    def _extract_keywords(self, text):
        """Extract keywords from text"""
        # Remove common stop words and extract meaningful terms
        stop_words = {
            'the','a','an','and','or','but','in','on','at','to','for','of','with','by','is','are','was','were','be','been','being',
            'have','has','had','do','does','did','will','would','could','should','breaking','news','update','report','says','after',
            'from','as','over','under','into','than','then','new','old','amid','amidst','vs','vs.'
        }

        # Preserve original case for proper noun detection
        original_tokens = re.findall(r"[A-Za-z][\w\-']+", text)
        lower_tokens = [t.lower() for t in original_tokens]

        # Unigrams excluding stopwords
        unigrams = [t for t in lower_tokens if t not in stop_words and len(t) > 2]

        # Simple proper noun capture (capitalized words in original)
        proper_nouns = [t for t in original_tokens if t[:1].isupper() and t.lower() not in stop_words]

        # Bigrams of informative words
        bigrams = []
        for i in range(len(unigrams) - 1):
            if unigrams[i] not in stop_words and unigrams[i+1] not in stop_words:
                bigrams.append(f"{unigrams[i]} {unigrams[i+1]}")

        # Priority: proper nouns and bigrams, then unigrams
        prioritized = []
        prioritized.extend(list(dict.fromkeys([p.lower() for p in proper_nouns])))
        prioritized.extend(list(dict.fromkeys(bigrams)))
        prioritized.extend(list(dict.fromkeys(unigrams)))

        # Return top tokens/phrases
        return prioritized[:10]

    def _normalize_text(self, text: str) -> str:
        """Normalize headline text for comparison and search."""
        if not text:
            return ''
        cleaned = re.sub(r"\s+", " ", text).strip()
        # Remove trailing punctuation that often varies across outlets
        cleaned = re.sub(r"[\s\-–—:]+$", "", cleaned)
        return cleaned
