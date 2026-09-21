// Copyright (C) CVAT.ai Corporation
//
// SPDX-License-Identifier: MIT

import './styles.scss';

import React, { useCallback, useEffect, useState } from 'react';
import { useParams } from 'react-router';
import Alert from 'antd/lib/alert';
import Badge from 'antd/lib/badge';
import Button from 'antd/lib/button';
import Card from 'antd/lib/card';
import Empty from 'antd/lib/empty';
import Statistic from 'antd/lib/statistic';
import Text from 'antd/lib/typography/Text';
import Title from 'antd/lib/typography/Title';
import { Col, Row } from 'antd/lib/grid';

import { SerializedClassCounts, getCore } from 'cvat-core-wrapper';
import GoBackButton from 'components/common/go-back-button';
import CVATLoadingSpinner from 'components/common/loading-spinner';
import ClassCountsChart from './class-counts-chart';
import useClassCountsSocket, { SocketStatus } from './use-class-counts-socket';

const core = getCore();

function ClassAnalyticsPage(): JSX.Element {
    const { tid } = useParams<{ tid: string }>();
    const taskId = +tid;

    const [data, setData] = useState<SerializedClassCounts | null>(null);
    const [fetching, setFetching] = useState(true);
    const [error, setError] = useState<string | null>(null);

    const fetchCounts = useCallback((options?: { silent?: boolean }) => {
        if (!Number.isInteger(taskId)) {
            setError(`"${tid}" is not a valid task id`);
            setFetching(false);
            return;
        }

        // A live refresh keeps the current chart on screen instead of
        // replacing it with a spinner on every annotation change.
        if (!options?.silent) {
            setFetching(true);
        }
        setError(null);

        core.analytics.classCounts.get(taskId).then((result: SerializedClassCounts) => {
            setData(result);
        }).catch((requestError: unknown) => {
            // Keep the message on the page rather than in a transient notification:
            // it is the page's primary content when the request fails.
            setError(requestError instanceof Error ? requestError.message : `${requestError}`);
            setData(null);
        }).finally(() => {
            setFetching(false);
        });
    }, [taskId, tid]);

    const onLiveChange = useCallback(() => {
        fetchCounts({ silent: true });
    }, [fetchCounts]);

    const socketStatus = useClassCountsSocket(taskId, { onChange: onLiveChange });

    useEffect(() => {
        fetchCounts();
    }, [fetchCounts]);

    const liveIndicator = {
        [SocketStatus.LIVE]: { status: 'success' as const, text: 'Live' },
        [SocketStatus.CONNECTING]: { status: 'processing' as const, text: 'Connecting' },
        [SocketStatus.RECONNECTING]: { status: 'warning' as const, text: 'Reconnecting' },
        [SocketStatus.OFFLINE]: { status: 'default' as const, text: 'Offline' },
    }[socketStatus];

    const renderContent = (): JSX.Element => {
        if (fetching) {
            return (
                <div className='cvat-class-analytics-loading'>
                    <CVATLoadingSpinner />
                </div>
            );
        }

        if (error !== null) {
            return (
                <Alert
                    className='cvat-class-analytics-error'
                    type='error'
                    showIcon
                    message='Could not load class statistics'
                    description={error}
                    action={<Button size='small' onClick={() => fetchCounts()}>Retry</Button>}
                />
            );
        }

        if (data === null) {
            return <Empty description='No data available' />;
        }

        const annotated = data.classes.some((item) => item.image_count > 0);

        return (
            <>
                <Row gutter={16} className='cvat-class-analytics-summary'>
                    <Col span={8}>
                        <Card>
                            <Statistic title='Total images' value={data.total_images} />
                        </Card>
                    </Col>
                    <Col span={8}>
                        <Card>
                            <Statistic title='Annotated images' value={data.annotated_images} />
                        </Card>
                    </Col>
                    <Col span={8}>
                        <Card>
                            <Statistic title='Classes' value={data.classes.length} />
                        </Card>
                    </Col>
                </Row>

                <Card className='cvat-class-analytics-chart-card' title='Images per class'>
                    { data.classes.length === 0 ? (
                        <Empty description='This task has no labels yet' />
                    ) : (
                        <>
                            { !annotated && (
                                <Alert
                                    className='cvat-class-analytics-empty-hint'
                                    type='info'
                                    showIcon
                                    message='No annotations yet'
                                    description={
                                        'Every label currently has zero images. ' +
                                        'Annotate some frames and reload this page.'
                                    }
                                />
                            )}
                            <ClassCountsChart classes={data.classes} totalImages={data.total_images} />
                            <div className='cvat-class-analytics-legend'>
                                { data.classes.map((item) => (
                                    <Text key={item.label_id} className='cvat-class-analytics-legend-item'>
                                        <span
                                            className='cvat-class-analytics-legend-swatch'
                                            style={{ background: item.color || '#1890ff' }}
                                        />
                                        {`${item.label_name}: ${item.image_count}`}
                                    </Text>
                                ))}
                            </div>
                        </>
                    )}
                </Card>
            </>
        );
    };

    return (
        <div className='cvat-class-analytics-page'>
            <Row justify='center'>
                <Col span={22} xl={18} xxl={14} className='cvat-task-top-bar'>
                    <GoBackButton />
                </Col>
            </Row>
            <Row justify='center'>
                <Col span={22} xl={18} xxl={14}>
                    <div className='cvat-class-analytics-header'>
                        <div className='cvat-class-analytics-title-row'>
                            <Title level={4}>Class statistics</Title>
                            <Badge
                                className='cvat-class-analytics-live-badge'
                                status={liveIndicator.status}
                                text={liveIndicator.text}
                            />
                        </div>
                        <Text type='secondary'>
                            {`Number of images containing each class in task #${tid}`}
                        </Text>
                    </div>
                    { renderContent() }
                </Col>
            </Row>
        </div>
    );
}

export default React.memo(ClassAnalyticsPage);
