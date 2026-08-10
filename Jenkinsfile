pipeline {
    agent { label 'wsl' }

    options {
        buildDiscarder(logRotator(numToKeepStr: '20'))
        timestamps()
        timeout(time: 45, unit: 'MINUTES')
    }

    parameters {
        string(name: 'MYSQL_HOST', defaultValue: '127.0.0.1', description: 'MySQL host reachable from WSL.')
        string(name: 'MYSQL_USER', defaultValue: 'swt', description: 'MySQL test user.')
        password(name: 'MYSQL_PASSWORD', defaultValue: 'swt-test-db-password', description: 'MySQL test password.')
        string(name: 'MYSQL_DATABASE', defaultValue: 'swt_test', description: 'Disposable test database.')
    }

    environment {
        DB_BACKEND = 'mysql'
        DATABASE_URL = ''
        APP_SECRET_KEY = 'jenkins-dev-only-secret-key-aaaaaaaaaaaaaaaa'
        LOGIN_PASSWORD = 'jenkins-dev-admin-password'
    }

    stages {
        stage('Dev') {
            steps {
                dir('swt_flask_project') {
                    sh '''
                        SWT_COMPOSE_PROJECT_NAME="$(printf 'swt-%s-%s' "$JOB_BASE_NAME" "$BUILD_NUMBER" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9_-' '-')"
                        export SWT_TEST_MYSQL_PORT=0
                        export SWT_TEST_MYSQL_USER="$MYSQL_USER"
                        export SWT_TEST_MYSQL_PASSWORD="$MYSQL_PASSWORD"
                        export SWT_TEST_MYSQL_DATABASE="$MYSQL_DATABASE"
                        python3 -m venv .jenkins-venv
                        .jenkins-venv/bin/python -m pip install --disable-pip-version-check -q -r requirements.txt -r requirements-dev.txt
                        .jenkins-venv/bin/python -m pip check
                        docker compose -p "$SWT_COMPOSE_PROJECT_NAME" -f ../swt_test_cases_project/docker-compose.test.yml build mysql
                        docker compose -p "$SWT_COMPOSE_PROJECT_NAME" -f ../swt_test_cases_project/docker-compose.test.yml up -d mysql
                        MYSQL_PORT="$(docker compose -p "$SWT_COMPOSE_PROJECT_NAME" -f ../swt_test_cases_project/docker-compose.test.yml port mysql 3306 | awk -F: 'END {print $NF}')"
                        {
                            printf 'export SWT_COMPOSE_PROJECT_NAME=%s\n' "$SWT_COMPOSE_PROJECT_NAME"
                            printf 'export MYSQL_PORT=%s\n' "$MYSQL_PORT"
                            printf 'export SWT_TEST_MYSQL_PORT=%s\n' "$MYSQL_PORT"
                        } > .jenkins-compose.env
                        . ./.jenkins-compose.env
                        for attempt in $(seq 1 60); do
                            if .jenkins-venv/bin/python -c 'import os, pymysql; connection = pymysql.connect(host=os.environ["MYSQL_HOST"], port=int(os.environ["MYSQL_PORT"]), user=os.environ["MYSQL_USER"], password=os.environ["MYSQL_PASSWORD"], database=os.environ["MYSQL_DATABASE"]); connection.close()' 2>/dev/null; then
                                break
                            fi
                            if [ "$attempt" -eq 60 ]; then
                                docker compose -p "$SWT_COMPOSE_PROJECT_NAME" -f ../swt_test_cases_project/docker-compose.test.yml logs --tail=150 mysql
                                exit 1
                            fi
                            sleep 2
                        done
                    '''
                }
            }
        }

        stage('Test') {
            steps {
                dir('swt_flask_project') {
                    sh '''
                        . ./.jenkins-compose.env
                        mkdir -p reports
                        .jenkins-venv/bin/python -m pytest -q --junitxml=reports/flask.xml
                    '''
                }
            }
            post {
                always {
                    junit allowEmptyResults: true, testResults: 'swt_flask_project/reports/*.xml'
                }
            }
        }

        stage('Build') {
            steps {
                sh '''
                    mkdir -p artifacts
                    git archive --format=zip --output="artifacts/swt-flask-final-${BUILD_NUMBER}.zip" HEAD:swt_flask_project
                '''
            }
            post {
                success {
                    archiveArtifacts artifacts: 'artifacts/swt-flask-final-*.zip', fingerprint: true
                }
            }
        }
    }

    post {
        always {
            sh '''
                SWT_COMPOSE_PROJECT_NAME="$(printf 'swt-%s-%s' "$JOB_BASE_NAME" "$BUILD_NUMBER" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9_-' '-')"
                docker compose -p "$SWT_COMPOSE_PROJECT_NAME" -f swt_test_cases_project/docker-compose.test.yml down -v --remove-orphans || true
            '''
            deleteDir()
        }
    }
}
