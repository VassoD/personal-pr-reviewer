# app.py
from flask import Flask, request, jsonify
from github import Github
import hmac
import hashlib
import os
import json
import base64
from github import GithubIntegration
import requests

app = Flask(__name__)

# GitHub App credentials
GITHUB_APP_ID = os.environ.get('GITHUB_APP_ID')
GITHUB_PRIVATE_KEY = os.environ.get('GITHUB_PRIVATE_KEY')
GITHUB_WEBHOOK_SECRET = os.environ.get('GITHUB_WEBHOOK_SECRET')
MISTRAL_API_KEY = os.environ.get('MISTRAL_API_KEY')

MISTRAL_API_URL = "https://api.mistral.ai/v1/chat/completions"

def verify_webhook(request):
    signature = request.headers.get('X-Hub-Signature-256')
    if not signature:
        return False
    
    expected_signature = 'sha256=' + hmac.new(
        GITHUB_WEBHOOK_SECRET.encode('utf-8'),
        request.data,
        hashlib.sha256
    ).hexdigest()
    
    return hmac.compare_digest(signature, expected_signature)

def get_github_client(installation_id):
    integration = GithubIntegration(
        GITHUB_APP_ID,
        GITHUB_PRIVATE_KEY
    )
    
    # Get an access token for the installation
    access_token = integration.get_access_token(installation_id).token
    return Github(access_token)

def analyze_code(file_content, file_name):
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {MISTRAL_API_KEY}"
    }
    
    system_prompt = """You are an expert software developer conducting code reviews. 
    Provide concise, actionable feedback focusing on code quality, best practices, and potential improvements. 
    Format your review in clear sections for positive aspects and suggestions."""
    
    user_prompt = f"""Review this code change in {file_name}:

{file_content}

Analyze the code for:
1. Good practices and improvements implemented
2. Potential issues or areas for improvement
3. Security concerns if any
4. Performance considerations

Provide your review in this format:
1. Positive points: [Brief list of good implementations]
2. Key suggestions: [Prioritized list of improvements]
3. Code example: [If applicable, show a brief example of suggested improvement]
4. Summary: [One-line overview of code quality]"""

    data = {
        "model": "mistral-large-latest",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "max_tokens": 1000,
        "temperature": 0.7
    }
    
    try:
        response = requests.post(MISTRAL_API_URL, headers=headers, json=data)
        response.raise_for_status()
        
        review = response.json()["choices"][0]["message"]["content"]
        return review
    except Exception as e:
        return f"Error analyzing code: {str(e)}"

@app.route('/webhook', methods=['POST'])
def webhook():
    if not verify_webhook(request):
        return jsonify({'error': 'Invalid signature'}), 403

    event = request.headers.get('X-GitHub-Event')
    if event != 'pull_request':
        return jsonify({'status': 'skipped', 'reason': f'Event {event} not handled'}), 200

    payload = request.json
    action = payload['action']
    
    if action not in ['opened', 'synchronize']:
        return jsonify({'status': 'skipped', 'reason': f'Action {action} not handled'}), 200

    installation_id = payload['installation']['id']
    repo_name = payload['repository']['full_name']
    pr_number = payload['pull_request']['number']
    
    gh = get_github_client(installation_id)
    repo = gh.get_repo(repo_name)
    pull = repo.get_pull(pr_number)

    # Store all reviews to post a single combined comment
    reviews = []
    
    # Get changed files
    for file in pull.get_files():
        if not file.filename.endswith(('.py', '.js', '.ts', '.tsx', '.jsx', '.vue', '.go', '.java', '.rb')):
            continue
            
        try:
            # Get file content
            file_content = base64.b64decode(
                repo.get_contents(file.filename, ref=pull.head.sha).content
            ).decode('utf-8')
            
            # Analyze code
            review_comment = analyze_code(file_content, file.filename)
            reviews.append(f"### Review for `{file.filename}`:\n\n{review_comment}\n\n---\n\n")
            
        except Exception as e:
            reviews.append(f"Error reviewing `{file.filename}`: {str(e)}\n\n---\n\n")

    if reviews:
        # Combine all reviews into a single comment
        combined_review = "# Code Review Summary\n\n" + "".join(reviews)
        pull.create_issue_comment(combined_review)

    return jsonify({'status': 'success'}), 200

if __name__ == '__main__':
    app.run(port=3000)

